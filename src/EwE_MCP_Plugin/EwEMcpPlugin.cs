using System;
using System.Collections.Generic;
using System.Data;
using System.Data.OleDb;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;
using EwECore;
using EwEPlugin;
using Newtonsoft.Json.Linq;
using ScientificInterfaceShared.Commands;
using ScientificInterfaceShared.Controls;

namespace EwE_MCP_Plugin
{
    public sealed class EwEMcpPlugin : IPlugin, IUIContextPlugin, IDisposedPlugin
    {
        private readonly object _gate = new object();
        private cCore _core;
        private cUIContext _uiContext;
        private cCommandHandler _commandHandler;
        private SynchronizationContext _syncContext;
        private EwEMcpSocketServer _server;
        private readonly EwEMcpAuditLog _auditLog = new EwEMcpAuditLog();

        public string Name => "EwEMCPPlugin";
        public string DisplayName => "EwE MCP Bridge";
        public string Description => "Local authenticated MCP bridge for controlling a running EwE instance.";
        public string Author => "Dexter Fichuk / Codex";
        public string Contact => "";

        public EwEMcpPlugin()
        {
            EwEMcpFileLog.Info("Plugin constructed");
        }

        public void Initialize(object core)
        {
            lock (_gate)
            {
                EwEMcpFileLog.Info($"Initialize called with core={core?.GetType().FullName ?? "<null>"}");
                _core = core as cCore;
                EnsureServer();
            }
        }

        public void UIContext(object uic)
        {
            lock (_gate)
            {
                EwEMcpFileLog.Info($"UIContext called with uic={uic?.GetType().FullName ?? "<null>"}");
                _uiContext = uic as cUIContext;
                if (_uiContext != null)
                {
                    _core = _uiContext.Core ?? _core;
                    _commandHandler = _uiContext.CommandHandler;
                    _syncContext = _uiContext.SyncObject;
                }
                EnsureServer();
            }
        }

        public void Dispose()
        {
            lock (_gate)
            {
                EwEMcpFileLog.Info("Dispose called");
                _server?.Stop();
                _server = null;
            }
        }

        private void EnsureServer()
        {
            if (_server != null)
            {
                return;
            }

            var config = EwEMcpConfig.FromEnvironment();

            var asm = typeof(EwEMcpPlugin).Assembly;
            var asmVersion = asm.GetName().Version?.ToString();
            var ewePath = AppDomain.CurrentDomain.BaseDirectory;

            if (!config.Enabled)
            {
                EwEMcpFileLog.Info($"EwE MCP server is disabled (EWE_MCP_ENABLED=0). Version: {asmVersion}, Location: {asm.Location}, EwE: {ewePath}");
                return;
            }

            EwEMcpFileLog.Info($"Starting socket server on {config.Host}:{config.Port}. Version: {asmVersion}, Location: {asm.Location}, EwE: {ewePath}");
            try
            {
                var router = new EwEMcpCommandRouter(
                    () => _core,
                    () => _uiContext,
                    () => _commandHandler,
                    () => _syncContext,
                    _auditLog,
                    config);
                router.OnShutdownRequested = () =>
                {
                    lock (_gate)
                    {
                        _server?.Stop();
                        _server = null;
                    }
                };
                _server = new EwEMcpSocketServer(config, router, _auditLog);
                _server.Start();
                EwEMcpFileLog.Info("Socket server started");
            }
            catch (System.Net.Sockets.SocketException ex)
            {
                EwEMcpFileLog.Error($"Socket server failed to bind to {config.Host}:{config.Port} (Port in use or invalid host)", ex);
                _server = null;
                throw;
            }
            catch (Exception ex)
            {
                EwEMcpFileLog.Error("Socket server failed to start", ex);
                _server = null;
                throw;
            }
        }
    }

    internal static class EwEMcpFileLog
    {
        private static readonly object Gate = new object();

        public static string Path => GetPath();

        public static void Info(string message)
        {
            Write("INFO", message, null);
        }

        public static void Warn(string message)
        {
            Write("WARN", message, null);
        }

        public static void Error(string message, Exception exception)
        {
            Write("ERROR", message, exception);
        }

        private static void Write(string level, string message, Exception exception)
        {
            try
            {
                lock (Gate)
                {
                    File.AppendAllText(GetPath(),
                        $"{DateTime.UtcNow:o} [{level}] {message}{Environment.NewLine}{exception}{Environment.NewLine}");
                }
            }
            catch
            {
                // Logging must never affect EwE plugin loading.
            }
        }

        private static string GetPath()
        {
            var root = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            if (string.IsNullOrWhiteSpace(root))
            {
                root = System.IO.Path.GetTempPath();
            }

            var dir = System.IO.Path.Combine(root, "Ecopath with Ecosim");
            Directory.CreateDirectory(dir);
            return System.IO.Path.Combine(dir, "EwE_MCP_Plugin.log");
        }
    }

    internal sealed class EwEMcpCommandRouter
    {
        private const string NavigationTreeName = "m_tvNavigation";
        private const string NodeBasicInput = "ndBasicInput";
        private const string NodeDietComposition = "ndDietComposition";
        private const string NodeFleetDefinition = "ndDefFleets";
        private const string NodeLandings = "ndLandings";
        private const string NodeDiscards = "ndDiscards";
        private const string NodeEcosimGroupInfo = "ndGroupInfo";
        private const string NodeEcosimFleetSizeDynamics = "ndFleetSizeDynamics";

        private readonly Func<cCore> _core;
        private readonly Func<cUIContext> _uiContext;
        private readonly Func<cCommandHandler> _commandHandler;
        private readonly Func<SynchronizationContext> _syncContext;
        private readonly EwEMcpAuditLog _auditLog;
        private readonly EwEMcpConfig _config;
        private HashSet<string> _dialogOpeningCommands;

        // ── DietComp persistence (new groups) ──
        // Tracks diet_comp writes for newly created Ecopath groups whose DC array
        // entries are not persisted by SaveEcopathDietComp during core.Save().
        // After core.Save() flushes the in-memory DC array (which has zeros for
        // new groups), FlushPendingDietWrites re-inserts these entries via SQL.
        private readonly Dictionary<(int, int), float> _pendingDietWrites = new Dictionary<(int, int), float>();
        private readonly object _dietLock = new object();

        public Action OnShutdownRequested { get; set; }

        public EwEMcpCommandRouter(
            Func<cCore> core,
            Func<cUIContext> uiContext,
            Func<cCommandHandler> commandHandler,
            Func<SynchronizationContext> syncContext,
            EwEMcpAuditLog auditLog,
            EwEMcpConfig config)
        {
            _core = core;
            _uiContext = uiContext;
            _commandHandler = commandHandler;
            _syncContext = syncContext;
            _auditLog = auditLog;
            _config = config;
        }

        public JObject Dispatch(EwEMcpRequest request)
        {
            switch ((request.Type ?? string.Empty).Trim().ToLowerInvariant())
            {
                case "ping":
                    return new JObject
                    {
                        ["pong"] = true,
                        ["plugin"] = "EwE MCP Bridge"
                    };
                case "diagnose":
                    return Diagnose();
                case "get_ewe_info":
                    return GetEwEInfo();
                case "get_runtime_state":
                    return GetRuntimeState();
                case "get_message_log":
                    return new JObject { ["entries"] = JArray.FromObject(_auditLog.Entries) };
                case "get_command_catalog":
                case "list_ui_commands":
                    return ListUiCommands();
                case "invoke_ui_command":
                    return OnUiThread(() => InvokeUiCommand(request.Params));
                case "list_navigation_nodes":
                    return OnUiThread(ListNavigationNodes);
                case "navigate_to_node":
                    return OnUiThread(() => NavigateToNode(request.Params));
                case "get_active_view":
                    return OnUiThread(GetActiveView);
                case "load_model":
                    return OnUiThread(() => LoadModel(request.Params));
                case "create_model":
                    return OnUiThread(() => CreateModel(request.Params));
                case "save_model":
                    return OnUiThread(() => SaveModel(request.Params, false));
                case "save_model_as":
                    return OnUiThread(() => SaveModel(request.Params, true));
                case "close_model":
                    return OnUiThread(CloseModel);
                case "get_model_info":
                    return GetModelInfo();
                case "has_changes":
                    return HasChanges();
                case "discard_changes":
                    return OnUiThread(DiscardChanges);
                case "get_core_counters":
                    return GetCoreCounters();
                case "list_domains":
                    return ListDomains();
                case "list_entities":
                    return ListEntities(request.Params);
                case "get_entity":
                    return GetEntity(request.Params);
                case "list_variables":
                    return ListVariables(request.Params);
                case "get_variable_metadata":
                    return GetVariableMetadata(request.Params);
                case "get_values":
                    return GetValues(request.Params);
                case "validate_values":
                    return ValidateValues(request.Params);
                case "get_indexed_value":
                    return GetIndexedValue(request.Params);
            case "validate_indexed_value":
                return ValidateIndexedValue(request.Params);
            case "search_entities":
                return SearchEntities(request.Params);
            case "get_values_range":
                return GetValuesRange(request.Params);
            case "set_values_range":
                return OnUiThread(() => SetValuesRange(request.Params));
            case "list_ecopath_groups":
                    return ListEcopathGroups();
                case "get_ecopath_group":
                    return GetEcopathGroup(request.Params);
                case "list_ecopath_fleets":
                    return ListEcopathFleets();
            case "get_ecopath_fleet":
                return GetEcopathFleet(request.Params);
            case "create_ecopath_group":
                return OnUiThread(() => CreateEcopathGroup(request.Params));
case "create_ecopath_fleet":
    return OnUiThread(() => CreateEcopathFleet(request.Params));
case "import_time_series":
    return OnUiThread(() => ImportTimeSeries(request.Params));
case "list_ecosim_scenarios":
                    return ListEcosimScenarios();
                case "get_ecosim_scenario":
                    return GetEcosimScenario(request.Params);
                case "load_ecosim_scenario":
                    return OnUiThread(() => LoadEcosimScenario(request.Params));
                case "list_ecospace_scenarios":
                    return ListEcospaceScenarios();
                case "get_ecospace_scenario":
                    return GetEcospaceScenario(request.Params);
                case "load_ecospace_scenario":
                    return OnUiThread(() => LoadEcospaceScenario(request.Params));
                case "get_ecospace_run_state":
                    return GetEcospaceRunState();
                case "run_ecospace":
                    return OnUiThread(RunEcospace);
                case "stop_ecospace":
                    return OnUiThread(StopEcospace);
                case "list_ecospace_group_inputs":
                    return ListEcospaceGroupInputs();
                case "get_ecospace_group_input":
                    return GetEcospaceGroupInput(request.Params);
                case "list_ecospace_fleet_inputs":
                    return ListEcospaceFleetInputs();
                case "get_ecospace_fleet_input":
                    return GetEcospaceFleetInput(request.Params);
                case "get_ecospace_group_results":
                    return GetEcospaceGroupResults(request.Params);
                case "get_ecospace_fleet_results":
                    return GetEcospaceFleetResults(request.Params);
                case "list_ecospace_habitats":
                    return ListGenericEntities("ecospace_habitats");
                case "get_ecospace_habitat":
                    return GetEcospaceHabitat(request.Params);
                case "list_ecospace_mpas":
                    return ListGenericEntities("ecospace_mpas");
                case "get_ecospace_mpa":
                    return GetEcospaceMpa(request.Params);
                case "get_ecospace_basemap":
                    return GetEcospaceBasemap();
                case "get_ecospace_model_parameters":
                    return GetEcospaceModelParameters();
                case "create_ecospace_scenario":
                    return OnUiThread(() => CreateEcospaceScenario(request.Params));
                case "copy_ecospace_scenario":
                    return OnUiThread(() => CopyEcospaceScenario(request.Params));
                case "delete_ecospace_scenario":
                    return OnUiThread(() => DeleteEcospaceScenario(request.Params));
            case "save_ecospace_scenario":
                return OnUiThread(SaveEcospaceScenario);
            case "get_ecospace_map_summary":
                return GetEcospaceMapSummary();
            case "create_ecospace_mpa":
                return OnUiThread(() => CreateEcospaceMpa(request.Params));
            case "set_ecospace_mpa_cells":
                return OnUiThread(() => SetEcospaceMpaCells(request.Params));
            case "create_regions_from_mpas":
                return OnUiThread(() => CreateRegionsFromMPAs(request.Params));
            case "list_ecotracer_scenarios":
                    return ListEcotracerScenarios();
                case "get_ecotracer_scenario":
                    return GetEcotracerScenario(request.Params);
                case "load_ecotracer_scenario":
                    return OnUiThread(() => LoadEcotracerScenario(request.Params));
                case "get_ecotracer_run_state":
                    return GetEcotracerRunState();
                case "get_ecotracer_model_parameters":
                    return GetEcotracerModelParameters();
                case "list_ecotracer_group_inputs":
                    return ListEcotracerGroupInputs();
                case "get_ecotracer_group_input":
                    return GetEcotracerGroupInput(request.Params);
                case "get_ecotracer_group_results":
                    return GetEcotracerGroupResults(request.Params);
                case "create_ecotracer_scenario":
                    return OnUiThread(() => CreateEcotracerScenario(request.Params));
                case "copy_ecotracer_scenario":
                    return OnUiThread(() => CopyEcotracerScenario(request.Params));
                case "delete_ecotracer_scenario":
                    return OnUiThread(() => DeleteEcotracerScenario(request.Params));
                case "save_ecotracer_scenario":
                    return OnUiThread(SaveEcotracerScenario);
                case "close_ecotracer_scenario":
                    return OnUiThread(CloseEcotracerScenario);
                case "get_mse_parameters":
                    return GetMseParameters();
                case "list_mse_group_inputs":
                    return ListMseGroupInputs();
                case "get_mse_group_input":
                    return GetMseGroupInput(request.Params);
                case "list_mse_fleet_inputs":
                    return ListMseFleetInputs();
                case "get_mse_fleet_input":
                    return GetMseFleetInput(request.Params);
                case "get_mse_run_state":
                    return GetMseRunState();
                case "run_mse":
                    return OnUiThread(RunMse);
                case "stop_mse":
                    return OnUiThread(StopMse);
                case "get_mse_group_results":
                    return GetMseGroupResults(request.Params);
                case "get_mse_fleet_results":
                    return GetMseFleetResults(request.Params);
                case "get_monte_carlo_parameters":
                    return GetMonteCarloParameters();
                case "list_monte_carlo_groups":
                    return ListMonteCarloGroups();
                case "get_monte_carlo_group":
                    return GetMonteCarloGroup(request.Params);
                case "get_monte_carlo_run_state":
                    return GetMonteCarloRunState();
                case "run_monte_carlo":
                    return OnUiThread(RunMonteCarlo);
                case "stop_monte_carlo":
                    return OnUiThread(StopMonteCarlo);
                case "get_installed_plugins":
                    return GetInstalledPlugins();
                case "create_ecosim_scenario":
                    return OnUiThread(() => CreateEcosimScenario(request.Params));
                case "copy_ecosim_scenario":
                    return OnUiThread(() => CopyEcosimScenario(request.Params));
                case "delete_ecosim_scenario":
                    return OnUiThread(() => DeleteEcosimScenario(request.Params));
                case "save_ecosim_scenario":
                    return OnUiThread(SaveEcosimScenario);
                case "get_ecosim_run_state":
                    return GetEcosimRunState();
            case "list_ecosim_group_inputs":
                return ListEcosimGroupInputs();
            case "get_ecosim_group_input":
                return GetEcosimGroupInput(request.Params);
            case "get_predators_of":
                return GetPredatorsOf(request.Params);
            case "list_ecosim_fleet_inputs":
                return ListEcosimFleetInputs();
            case "get_ecosim_fleet_input":
                return GetEcosimFleetInput(request.Params);
            case "list_ecosim_forcing_shapes":
                return ListEcosimForcingShapes();
            case "get_ecosim_forcing_shape":
                return GetEcosimForcingShape(request.Params);
            case "set_ecosim_forcing_shape":
                return OnUiThread(() => SetEcosimForcingShape(request.Params));
            case "list_ecosim_pp_forcing":
                return ListEcosimPPForcing();
            case "get_ecosim_pp_forcing":
                return GetEcosimPPForcing(request.Params);
                case "get_ecosim_group_results":
                    return GetEcosimGroupResults(request.Params);
                case "get_ecosim_fleet_results":
                    return GetEcosimFleetResults(request.Params);
                case "export_ecosim_results":
                    return OnUiThread(() => ExportEcosimResults(request.Params));
                case "run_ecopath":
                    return OnUiThread(RunEcopath);
                case "get_ecopath_run_state":
                    return GetEcopathRunState();
                case "get_ecopath_stats":
                    return GetEcopathStats();
                case "get_ecopath_group_diagnostics":
                    return GetEcopathGroupDiagnostics();
                case "get_ecopath_fleet_diagnostics":
                    return GetEcopathFleetDiagnostics();
                case "get_mti_matrix":
                    return GetMtiMatrix();
                case "export_ecopath_results":
                    return OnUiThread(() => ExportEcopathResults(request.Params));
                case "run_ecosim":
                    return OnUiThread(RunEcosim);
                case "stop_ecosim":
                    return OnUiThread(StopEcosim);
                case "wait_for_ecosim_completion":
                    return OnUiThread(() => WaitForEcosimCompletion(request.Params));
                case "refresh_ui":
                    return OnUiThread(RefreshUI);
                case "capture_view":
                    return OnUiThread(() => CaptureView(request.Params));
                case "export_active_plot":
                    return OnUiThread(() => ExportActivePlot(request.Params));
                case "set_values":
                    return OnUiThread(() => SetValues(request.Params));
                case "set_indexed_value":
                    return OnUiThread(() => SetIndexedValue(request.Params));
                case "batch":
                case "batch_commands":
                    return OnUiThread(() => Batch(request.Params));
            case "execute_ewe_code":
                return OnUiThread(() => ExecuteEweCode(request.Params));
            case "shutdown_server":
                return ShutdownServer();
            case "get_batch_blocked_commands":
                return GetBatchBlockedCommands();
            default:
                    throw new EwEMcpException("unknown_command", $"Unknown command type: {request.Type}");
            }
        }

        private JObject OnUiThread(Func<JObject> func)
        {
            var sync = _syncContext();
            if (sync == null || SynchronizationContext.Current == sync)
            {
                return func();
            }

            JObject result = null;
            Exception error = null;
            sync.Send(_ =>
            {
                try
                {
                    result = func();
                }
                catch (Exception ex)
                {
                    error = ex;
                }
            }, null);
            if (error != null)
            {
                throw error;
            }
            return result;
        }

        private JObject Diagnose()
        {
            var core = _core();
            var uic = _uiContext();
            var cmdh = _commandHandler();
            return new JObject
            {
                ["status"] = core != null && uic != null ? "healthy" : "degraded",
                ["checks"] = new JArray
                {
                    Check("core", core != null, core?.GetType().FullName),
                    Check("ui_context", uic != null, uic?.GetType().FullName),
                    Check("command_handler", cmdh != null, cmdh?.GetType().FullName),
                    Check("plugin_log", File.Exists(EwEMcpFileLog.Path), EwEMcpFileLog.Path),
                    Check("token", !string.IsNullOrWhiteSpace(_config.Token), _config.UsingDefaultToken ? "default development token" : "configured"),
                    Check("eval_enabled", _config.EnableEval, _config.EnableEval ? "enabled" : "disabled"),
                Check("dialog_watcher_enabled", _config.DialogWatcherEnabled, _config.DialogWatcherEnabled ? "enabled" : "disabled")
                },
                ["host"] = _config.Host,
                ["port"] = _config.Port,
                ["config_file_path"] = _config.ConfigFilePath,
                ["config_file_exists"] = _config.ConfigFileExists,
                ["plugin"] = GetPluginAssemblyInfo()
            };
        }

        private static JObject Check(string name, bool ok, string detail)
        {
            return new JObject
            {
                ["name"] = name,
                ["status"] = ok ? "ok" : "error",
                ["detail"] = detail ?? string.Empty
            };
        }

        private JObject GetEwEInfo()
        {
            var core = RequireCore();
            var asm = core.GetType().Assembly;
            return new JObject
            {
                ["core_type"] = core.GetType().FullName,
                ["core_assembly"] = asm.GetName().Name,
                ["core_version"] = asm.GetName().Version?.ToString(),
                ["core_location"] = asm.Location,
                ["app_domain"] = AppDomain.CurrentDomain.FriendlyName,
                ["base_directory"] = AppDomain.CurrentDomain.BaseDirectory,
                ["plugin"] = GetPluginAssemblyInfo()
            };
        }

        private JObject GetRuntimeState()
        {
            var core = _core();
            return new JObject
            {
                ["has_core"] = core != null,
                ["has_ui_context"] = _uiContext() != null,
                ["has_command_handler"] = _commandHandler() != null,
                ["model"] = core == null ? null : GetModelInfo(),
                ["commands"] = ListUiCommands()
            };
        }

        private JObject GetModelInfo()
        {
            var core = RequireCore();
            var eweModel = SafeGetProperty(core, "EwEModel");
            var info = new JObject
            {
                ["loaded"] = SafeGetProperty(core, "DataSource") != null,
                ["data_source"] = SafeGetProperty(core, "DataSource")?.ToString(),
                ["model_name"] = eweModel?.GetType().GetProperty("Name")?.GetValue(eweModel)?.ToString()
            };

        if (eweModel != null)
            {
                // Explicit property list avoids reflection over all cEwEModel properties,
                // which could pick up complex objects, indexed properties, or future
                // array properties that would bloat the response.
                var modelProps = new[]
                {
                    "Name", "Description", "Author", "Contact", "Area",
                    "NumDigits", "GroupDigits", "FirstYear", "NumYears",
                    "South", "North", "West", "East",
                    "UnitTimeCustomText", "UnitCurrencyCustomText", "UnitMonetary",
                    "UnitAreaCustomText", "LastSaved", "IsEcoSpaceModelCoupled",
                    "Country", "EcosystemType", "EcobaseCode",
                    "PublicationDOI", "PublicationURI", "PublicationReference"
                };
                var metadata = new JObject();
                foreach (var propName in modelProps)
                {
                    try
                    {
                        var val = eweModel.GetType().GetProperty(propName)?.GetValue(eweModel);
                        if (val != null) metadata[ToVariableName(propName)] = ToJToken(val);
                    }
                    catch { }
                }
                info["metadata"] = metadata;
            }
            return info;
        }

        private JObject GetCoreCounters()
        {
            var core = RequireCore();
            var result = new JObject();
            var method = core.GetType().GetMethods()
                .FirstOrDefault(m => m.Name == "GetCoreCounter" && m.GetParameters().Length == 1 && m.GetParameters()[0].ParameterType.IsEnum);
            var enumType = method?.GetParameters()[0].ParameterType;
            if (enumType == null)
            {
                result["nGroups"] = SafeIntProperty(core, "nGroups");
                result["nLivingGroups"] = SafeIntProperty(core, "nLivingGroups");
                result["nFleets"] = SafeIntProperty(core, "nFleets");
                result["nDetritus"] = SafeIntProperty(core, "nDetritus");
                result["nEcosimYears"] = SafeIntProperty(core, "nEcosimYears");
                return result;
            }

            foreach (var value in Enum.GetValues(enumType))
            {
                try
                {
                    var count = method.Invoke(core, new[] { value });
                    result[value.ToString()] = count == null ? JValue.CreateNull() : JToken.FromObject(count);
                }
                catch
                {
                    // Some counters may not be valid in every runtime state.
                }
            }
            return result;
        }

        private JObject ListDomains()
        {
            return new JObject
            {
                ["domains"] = new JArray
                {
                    Domain("ecopath_groups", "Ecopath functional groups and read-only Ecopath input/output values", true, true),
                    Domain("ecopath_group_outputs", "Ecopath functional group output values from the latest Ecopath run", true, true),
                    Domain("ecopath_fleets", "Ecopath fleets with landings/discards by group", true, true),
                    Domain("ecopath_fleet_outputs", "Ecopath fleet output values from the latest Ecopath run", true, true),
                    Domain("ecopath_stats", "Ecopath system-level statistics from the latest Ecopath run", true, true),
                    Domain("ecosim_scenarios", "Ecosim scenarios", true, true),
                    Domain("ecosim_group_inputs", "Ecosim group input parameters", true, true),
                    Domain("ecosim_group_outputs", "Ecosim group output time-series/results", true, true),
                    Domain("ecosim_fleet_inputs", "Ecosim fleet input parameters", true, true),
                    Domain("ecosim_fleet_outputs", "Ecosim fleet output time-series/results", true, true),
                    Domain("ecospace_scenarios", "Ecospace scenarios", true, true),
                    Domain("ecospace_group_inputs", "Ecospace group input parameters", true, true),
                    Domain("ecospace_fleet_inputs", "Ecospace fleet input parameters", true, true),
                    Domain("ecospace_group_outputs", "Ecospace group output time-series/results", true, true),
                    Domain("ecospace_fleet_outputs", "Ecospace fleet output time-series/results", true, true),
                    Domain("ecospace_habitats", "Ecospace habitat definitions", true, true),
                    Domain("ecospace_mpas", "Ecospace marine protected area definitions", true, true),
                    Domain("ecospace_region_outputs", "Ecospace region output values", true, true),
                    Domain("ecotracer_scenarios", "Ecotracer scenarios", true, true),
                    Domain("ecotracer_group_inputs", "Ecotracer group input parameters", true, true),
                    Domain("ecotracer_model_parameters", "Ecotracer environment-level model parameters", true, true),
                    Domain("ecotracer_group_outputs", "Ecotracer group output concentrations", true, true),
                    Domain("mse_parameters", "MSE model parameters", true, true),
                    Domain("mse_group_inputs", "MSE group input parameters", true, true),
                    Domain("mse_fleet_inputs", "MSE fleet input parameters", true, true),
                    Domain("mse_group_outputs", "MSE group output results", true, true),
                    Domain("mse_fleet_outputs", "MSE fleet output results", true, true),
                    Domain("monte_carlo_groups", "Monte Carlo per-group uncertainty parameters", true, true),
                    Domain("taxa", "Taxonomy records linked to the model", true, true),
                    Domain("core", "Top-level EwE cCore public scalar properties", true, true),
                    Domain("ui_commands", "Commands registered with EwE's UI command handler", true, true)
                }
            };
        }

        private static JObject Domain(string name, string description, bool readable, bool implemented)
        {
            return new JObject
            {
                ["name"] = name,
                ["description"] = description,
                ["readable"] = readable,
                ["implemented"] = implemented
            };
        }

        private JObject ListEntities(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain").Trim().ToLowerInvariant();
            switch (domain)
            {
                case "ecopath_groups":
                case "ecopath_group":
                    return ListGenericEntities("ecopath_groups");
                case "ecopath_fleets":
                case "ecopath_fleet":
                    return ListGenericEntities("ecopath_fleets");
                case "ecopath_group_outputs":
                case "ecopath_group_output":
                    return ListGenericEntities("ecopath_group_outputs");
                case "ecopath_fleet_outputs":
                case "ecopath_fleet_output":
                    return ListGenericEntities("ecopath_fleet_outputs");
                case "ecopath_stats":
                case "ecopath_stat":
                    return ListGenericEntities("ecopath_stats");
                case "ecosim_scenarios":
                case "ecosim_scenario":
                case "ecosim_group_inputs":
                case "ecosim_group_input":
                case "ecosim_group_outputs":
                case "ecosim_group_output":
                case "ecosim_fleet_inputs":
                case "ecosim_fleet_input":
                case "ecosim_fleet_outputs":
                case "ecosim_fleet_output":
                case "ecospace_scenarios":
                case "ecospace_scenario":
                case "ecospace_group_inputs":
                case "ecospace_group_input":
                case "ecospace_fleet_inputs":
                case "ecospace_fleet_input":
                case "ecospace_group_outputs":
                case "ecospace_group_output":
                case "ecospace_fleet_outputs":
                case "ecospace_fleet_output":
                case "ecospace_habitats":
                case "ecospace_habitat":
                case "ecospace_mpas":
                case "ecospace_mpa":
                case "ecospace_region_outputs":
                case "ecospace_region_output":
                case "ecotracer_scenarios":
                case "ecotracer_scenario":
                case "ecotracer_group_inputs":
                case "ecotracer_group_input":
                case "ecotracer_model_parameters":
                case "ecotracer_model_parameter":
                case "ecotracer_group_outputs":
                case "ecotracer_group_output":
                case "mse_group_inputs":
                case "mse_group_input":
                case "mse_fleet_inputs":
                case "mse_fleet_input":
                case "mse_parameters":
                case "mse_parameter":
                case "mse_group_outputs":
                case "mse_group_output":
                case "mse_fleet_outputs":
                case "mse_fleet_output":
                case "monte_carlo_groups":
                case "monte_carlo_group":
                case "taxa":
                case "taxon":
                    return ListGenericEntities(NormalizeDomain(domain));
                case "core":
                    return new JObject
                    {
                        ["entities"] = new JArray { BuildGenericEntity(ResolveEntity("core", null).Entity, "core", null) },
                        ["total_count"] = 1
                    };
                case "ui_commands":
                    return ListUiCommands();
                default:
                    throw new EwEMcpException("unknown_domain", $"Unknown domain: {domain}");
            }
        }

        private JObject GetEntity(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain");
            int? index = OptionalInt(parameters, "index");
            var target = ResolveEntity(domain, index);

            if (target.NormalizedDomain == "ecopath_groups")
            {
                return BuildEcopathGroup(RequireCore(), target.Index.Value, true);
            }
            if (target.NormalizedDomain == "ecopath_fleets")
            {
                return BuildEcopathFleet(RequireCore(), target.Index.Value, true);
            }

            return BuildGenericEntity(target.Entity, target.NormalizedDomain, target.Index);
        }

        private JObject ListGenericEntities(string domain)
        {
            var core = RequireCore();
            var entities = new JArray();
            var count = GetDomainCount(core, domain);
            for (var i = 1; i <= count; i++)
            {
                try
                {
                    var target = ResolveEntity(domain, i);
                    entities.Add(BuildGenericEntity(target.Entity, target.NormalizedDomain, target.Index));
                }
                catch (EwEMcpException ex)
                {
                    entities.Add(new JObject
                    {
                        ["domain"] = domain,
                        ["index"] = i,
                        ["error"] = new JObject
                        {
                            ["code"] = ex.Code,
                            ["message"] = ex.Message
                        }
                    });
                }
            }
            return new JObject
            {
                ["domain"] = domain,
                ["entities"] = entities,
                ["total_count"] = count,
                ["available_count"] = entities.Count(e => e["error"] == null)
            };
        }

        private JObject BuildGenericEntity(object entity, string domain, int? index)
        {
            if (entity == null)
            {
                throw new EwEMcpException("entity_unavailable", $"Entity unavailable for domain {domain}");
            }

            var result = new JObject
            {
                ["domain"] = domain,
                ["type"] = entity.GetType().FullName
            };
            if (index.HasValue)
            {
                result["index"] = index.Value;
            }

            var name = SafeStringProperty(entity, "Name");
            if (!string.IsNullOrWhiteSpace(name))
            {
                result["name"] = name;
            }

            result["values"] = ReadScalarValues(entity, null, false, domain);
            return result;
        }

        private JObject ListVariables(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain");
            int? index = OptionalInt(parameters, "index");
            var target = ResolveEntity(domain, index);
            var variables = new JArray();

            foreach (var property in GetReadableProperties(target.Entity))
            {
                variables.Add(PropertyMetadata(property, target.NormalizedDomain, target.Entity));
            }

            return new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["variables"] = variables,
                ["total_count"] = variables.Count
            };
        }

    private JObject GetVariableMetadata(JObject parameters)
    {
        var domain = RequiredString(parameters, "domain");
        var variable = RequiredString(parameters, "variable");
        int? index = OptionalInt(parameters, "index");
        var target = ResolveEntity(domain, index);
        var property = FindReadableProperty(target.Entity, variable, target.NormalizedDomain);
        if (property == null)
        {
            // Check if the property exists but is indexed
            var indexedProp = FindAsIndexedProperty(target.Entity, variable, target.NormalizedDomain);
            if (indexedProp != null)
            {
                return PropertyMetadata(indexedProp, target.NormalizedDomain, target.Entity);
            }
            throw new EwEMcpException("unknown_variable", $"Variable not found: {variable}");
        }
        return PropertyMetadata(property, target.NormalizedDomain, target.Entity);
    }

        private JObject GetValues(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain");
            int? index = OptionalInt(parameters, "index");
            var target = ResolveEntity(domain, index);
            var variables = OptionalStringArray(parameters, "variables");
            var navigation = OptionalBool(parameters, "navigate") ? NavigateToSuggestedEditNode(target.NormalizedDomain, variables) : null;

            var result = new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["values"] = ReadScalarValues(target.Entity, variables, true, target.NormalizedDomain)
            };
            if (navigation != null)
            {
                result["navigation"] = navigation;
            }
            return result;
        }

        private JObject SearchEntities(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain");
            var normalized = NormalizeDomain(domain);
            var nameContains = OptionalString(parameters, "name_contains")?.Trim();
            var propertyFilters = parameters["property_filters"] as JObject;
            var maxResults = Math.Min(OptionalInt(parameters, "max_results") ?? 100, 500);

            var core = RequireCore();
            var count = GetDomainCount(core, normalized);
            if (normalized == "core")
            {
                var entity = ResolveEntity("core", null);
                var row = BuildSearchRow(entity.Entity, normalized, null, propertyFilters);
                if (row != null) row["name"] = "core";
                return new JObject
                {
                    ["domain"] = normalized,
                    ["results"] = row != null ? new JArray { row } : new JArray(),
                    ["match_count"] = row != null ? 1 : 0,
                    ["total_count"] = 1
                };
            }

            var results = new JArray();
            var matched = 0;
            for (var i = 1; i <= count && matched < maxResults; i++)
            {
                object entity;
                try
                {
                    var target = ResolveEntity(normalized, i);
                    entity = target.Entity;
                }
                catch (EwEMcpException)
                {
                    continue;
                }

                var name = SafeStringProperty(entity, "Name") ?? "";

                if (!string.IsNullOrWhiteSpace(nameContains) &&
                    name.IndexOf(nameContains, StringComparison.OrdinalIgnoreCase) < 0)
                {
                    continue;
                }

                var row = BuildSearchRow(entity, normalized, i, propertyFilters);
                if (row == null) continue;

                row["index"] = i;
                if (!string.IsNullOrWhiteSpace(name)) row["name"] = name;
                results.Add(row);
                matched++;
            }

            return new JObject
            {
                ["domain"] = normalized,
                ["results"] = results,
                ["match_count"] = matched,
                ["total_count"] = count
            };
        }

        private JObject BuildSearchRow(object entity, string domain, int? index, JObject propertyFilters)
        {
            if (propertyFilters == null || propertyFilters.Count == 0)
            {
                return new JObject { ["domain"] = domain };
            }

            var values = ReadScalarValues(entity, propertyFilters.Properties().Select(p => p.Name).ToArray(), true, domain);
            foreach (var filter in propertyFilters)
            {
                var filterVal = filter.Value?.ToString()?.Trim();
                if (string.IsNullOrWhiteSpace(filterVal)) continue;

                if (!values.ContainsKey(filter.Key)) return null;

                var actualVal = values[filter.Key];
                if (actualVal is JObject errorObj && errorObj["error"] != null) return null;

                var actualStr = actualVal?.ToString() ?? "";
                if (actualStr.IndexOf(filterVal, StringComparison.OrdinalIgnoreCase) < 0) return null;
            }

            return new JObject
            {
                ["domain"] = domain,
                ["values"] = values
            };
        }

        private JObject GetValuesRange(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain");
            var normalized = NormalizeDomain(domain);
            var variables = OptionalStringArray(parameters, "variables");
            var startIndex = OptionalInt(parameters, "start_index") ?? 1;
            var endIndex = OptionalInt(parameters, "end_index");
    var maxEntities = Math.Min(OptionalInt(parameters, "max_entities") ?? 50, 500);
    var navigate = OptionalBool(parameters, "navigate", false);

    var core = RequireCore();
    if (normalized == "core")
    {
        var target = ResolveEntity("core", null);
        return new JObject
        {
            ["domain"] = normalized,
            ["entities"] = new JArray
            {
                new JObject
                {
                    ["index"] = JValue.CreateNull(),
                    ["name"] = "core",
                    ["values"] = ReadScalarValues(target.Entity, variables, true, normalized)
                }
            },
            ["entity_count"] = 1,
            ["truncated"] = false
        };
    }

    var count = GetDomainCount(core, normalized);
    var effectiveEnd = endIndex ?? count;
    if (startIndex < 1) startIndex = 1;
    if (effectiveEnd > count) effectiveEnd = count;

    var results = new JArray();
    var truncated = false;
    var entityCount = 0;

    for (var i = startIndex; i <= effectiveEnd; i++)
    {
        if (entityCount >= maxEntities)
        {
            truncated = true;
            break;
        }

        try
        {
            var target = ResolveEntity(normalized, i);
            results.Add(new JObject
            {
                ["index"] = i,
                ["name"] = SafeStringProperty(target.Entity, "Name") ?? "",
                ["values"] = ReadScalarValues(target.Entity, variables, true, normalized)
            });
            entityCount++;
        }
        catch (EwEMcpException)
        {
            results.Add(new JObject
            {
                ["index"] = i,
                ["name"] = "",
                ["error"] = "entity_unavailable"
            });
        }
    }

            return new JObject
            {
                ["domain"] = normalized,
                ["entities"] = results,
                ["entity_count"] = entityCount,
                ["start_index"] = startIndex,
                ["end_index"] = effectiveEnd,
                ["total_count"] = count,
                ["truncated"] = truncated
            };
        }

        private JObject SetValuesRange(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain");
            var normalized = NormalizeDomain(domain);
            var values = parameters["values"] as JObject ?? throw new EwEMcpException("missing_parameter", "Missing 'values' object");
            var startIndex = OptionalInt(parameters, "start_index") ?? 1;
            var endIndex = OptionalInt(parameters, "end_index");
    var maxEntities = Math.Min(OptionalInt(parameters, "max_entities") ?? 20, 500);
    var navigate = OptionalBool(parameters, "navigate", false);

    var core = RequireCore();
    if (normalized == "core")
    {
        var target = ResolveEntity("core", null);
        var singleResult = SetGenericValues(normalized, target.Entity, null, values);
        return new JObject
        {
            ["domain"] = normalized,
            ["entities"] = new JArray { singleResult },
            ["entity_count"] = 1,
            ["truncated"] = false
        };
    }

    var count = GetDomainCount(core, normalized);
    var effectiveEnd = endIndex ?? count;
    if (startIndex < 1) startIndex = 1;
    if (effectiveEnd > count) effectiveEnd = count;

    var results = new JArray();
    var truncated = false;
    var entityCount = 0;

    for (var i = startIndex; i <= effectiveEnd; i++)
    {
        if (entityCount >= maxEntities)
        {
            truncated = true;
            break;
        }

        try
        {
            var target = ResolveEntity(normalized, i);
            results.Add(SetGenericValues(normalized, target.Entity, i, values));
            entityCount++;
        }
        catch (EwEMcpException ex)
        {
            results.Add(new JObject
            {
                ["domain"] = normalized,
                ["index"] = i,
                ["name"] = "",
                ["results"] = new JObject { ["_entity_error"] = ex.Code }
            });
        }
    }

            if (navigate)
            {
                var navigation = NavigateToSuggestedEditNode(normalized, values.Properties().Select(p => p.Name));
                if (navigation != null && results.Count > 0)
                {
                    results[0]["navigation"] = navigation;
                }
            }

            return new JObject
            {
                ["domain"] = normalized,
                ["entities"] = results,
                ["entity_count"] = entityCount,
                ["start_index"] = startIndex,
                ["end_index"] = effectiveEnd,
                ["total_count"] = count,
                ["truncated"] = truncated
            };
        }

        private JObject ValidateValues(JObject parameters)
        {
            var domain = RequiredString(parameters, "domain");
            int? index = OptionalInt(parameters, "index");
            var values = parameters["values"] as JObject ?? throw new EwEMcpException("missing_parameter", "Missing 'values' object");
            var target = ResolveEntity(domain, index);
            var result = new JObject();

            foreach (var pair in values)
            {
                var property = FindWritableProperty(target.Entity, pair.Key, target.NormalizedDomain);
                if (property == null)
                {
                    // Check if the property exists but is indexed (requires
                    // validate_indexed_value instead of validate_values).
                    var indexedProp = FindAsIndexedProperty(target.Entity, pair.Key, target.NormalizedDomain);
                    if (indexedProp != null)
                    {
                        var indexParams = indexedProp.GetIndexParameters();
                        var paramNames = string.Join(", ", indexParams.Select(p => p.Name));
                        result[pair.Key] = new JObject
                        {
                            ["valid"] = false,
                            ["reason"] = "indexed_variable",
                            ["message"] = $"Variable '{pair.Key}' is indexed ({indexParams.Length} index parameter(s): {paramNames}). Use validate_indexed_value instead of validate_values."
                        };
                    }
                    else
                    {
                        result[pair.Key] = new JObject
                        {
                            ["valid"] = false,
                            ["reason"] = "unknown_or_readonly_variable"
                        };
                    }
                    continue;
                }

                result[pair.Key] = new JObject
                {
                    ["valid"] = TryConvertValue(pair.Value, property.PropertyType, out var convertedValue),
                    ["target_property"] = property.Name,
                    ["type"] = FriendlyTypeName(property.PropertyType)
                };
                var rangeError = ValidatePropertyValue(target.Entity, property, convertedValue);
                if (rangeError != null)
                {
                    result[pair.Key]["valid"] = false;
                    result[pair.Key]["reason"] = rangeError;
                }
            }

            return new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["results"] = result
            };
        }

        private JObject GetIndexedValue(JObject parameters)
        {
            var target = ResolveEntity(RequiredString(parameters, "domain"), OptionalInt(parameters, "index"));
            var variable = RequiredString(parameters, "variable");
            var indices = RequiredIntArray(parameters, "indices");
            var navigation = OptionalBool(parameters, "navigate") ? NavigateToSuggestedEditNode(target.NormalizedDomain, new[] { variable }) : null;
            var property = FindIndexedProperty(target.Entity, variable, target.NormalizedDomain, indices.Length, requireWritable: false);
            if (property == null)
            {
                throw new EwEMcpException("unknown_indexed_variable", $"Indexed variable not found: {variable} with {indices.Length} index value(s)");
            }
            ValidateIndexedBounds(target, property, indices);

            var value = ReadIndexedPropertyValue(target.Entity, property, indices);
            var result = new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["variable"] = ToVariableName(property.Name),
                ["property"] = property.Name,
                ["indices"] = JArray.FromObject(indices),
                ["value"] = value
            };
            if (navigation != null)
            {
                result["navigation"] = navigation;
            }
            return result;
        }

        private JObject ValidateIndexedValue(JObject parameters)
        {
            var target = ResolveEntity(RequiredString(parameters, "domain"), OptionalInt(parameters, "index"));
            var variable = RequiredString(parameters, "variable");
            var indices = RequiredIntArray(parameters, "indices");
            var value = parameters["value"] ?? throw new EwEMcpException("missing_parameter", "Missing value");
            var property = FindIndexedProperty(target.Entity, variable, target.NormalizedDomain, indices.Length, requireWritable: true);
            if (property == null)
            {
                return IndexedValidationResult(target, variable, indices, false, "unknown_or_readonly_indexed_variable", null);
            }
            var boundsError = GetIndexedBoundsError(target, property, indices);
            if (!string.IsNullOrWhiteSpace(boundsError))
            {
                return IndexedValidationResult(target, property.Name, indices, false, boundsError, property);
            }

            var valid = TryConvertValue(value, property.PropertyType, out var convertedValue);
            if (!valid)
            {
                return IndexedValidationResult(target, property.Name, indices, false, "conversion_failed", property);
            }
            var rangeError = ValidatePropertyValue(target.Entity, property, convertedValue);
            if (rangeError != null)
            {
                return IndexedValidationResult(target, property.Name, indices, false, rangeError, property);
            }
            return IndexedValidationResult(target, property.Name, indices, true, string.Empty, property);
        }

        private JObject ListEcopathGroups()
        {
            var core = RequireCore();
            var groups = new JArray();
            var count = SafeIntProperty(core, "nGroups");
            var livingCount = SafeIntProperty(core, "nLivingGroups");
            for (var i = 1; i <= count; i++)
            {
                groups.Add(BuildEcopathGroup(core, i, false));
            }

            return new JObject
            {
                ["groups"] = groups,
                ["total_count"] = count,
                ["living_count"] = livingCount
            };
        }

        private JObject GetEcopathGroup(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
            var count = SafeIntProperty(core, "nGroups");
            if (index < 1 || index > count)
            {
                throw new EwEMcpException("index_out_of_range", $"Group index {index} is outside 1..{count}");
            }
            return BuildEcopathGroup(core, index, true);
        }

        private JObject BuildEcopathGroup(cCore core, int index, bool includeDiet)
        {
            var input = SafeGetIndexedProperty(core, "EcopathGroupInputs", index);
            var output = SafeGetIndexedProperty(core, "EcopathGroupOutputs", index);
            var group = new JObject
            {
                ["index"] = index,
                ["name"] = SafeStringProperty(input, "Name"),
                ["type"] = GroupType(input),
                ["is_living"] = !SafeBoolProperty(input, "IsDetritus"),
                ["input"] = new JObject
                {
                    ["biomass_area"] = SafeJTokenProperty(input, "BiomassAreaInput"),
                    ["pb"] = SafeJTokenProperty(input, "PBInput"),
                    ["qb"] = SafeJTokenProperty(input, "QBInput"),
                    ["ee"] = SafeJTokenProperty(input, "EEInput")
                },
                ["output"] = new JObject
                {
                    ["biomass"] = SafeJTokenProperty(output, "Biomass"),
                    ["biomass_area"] = SafeJTokenProperty(output, "BiomassArea")
                }
            };

            if (includeDiet)
            {
                var diet = new JArray();
                var preyCount = SafeIntProperty(core, "nGroups");
                for (var prey = 1; prey <= preyCount; prey++)
                {
                    var amount = SafeGetIndexedProperty(input, "DietComp", prey);
                    if (IsNonZero(amount))
                    {
                        var preyInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", prey);
                        diet.Add(new JObject
                        {
                            ["prey_index"] = prey,
                            ["prey_name"] = SafeStringProperty(preyInput, "Name"),
                            ["amount"] = ToJToken(amount)
                        });
                    }
                }
                group["diet"] = diet;
            }

            return group;
        }

        private JObject ListEcopathFleets()
        {
            var core = RequireCore();
            var fleets = new JArray();
            var count = SafeIntProperty(core, "nFleets");
            for (var i = 1; i <= count; i++)
            {
                fleets.Add(BuildEcopathFleet(core, i, false));
            }
            return new JObject { ["fleets"] = fleets, ["total_count"] = count };
        }

        private JObject GetEcopathFleet(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
            var count = SafeIntProperty(core, "nFleets");
            if (index < 1 || index > count)
            {
                throw new EwEMcpException("index_out_of_range", $"Fleet index {index} is outside 1..{count}");
            }
            return BuildEcopathFleet(core, index, true);
        }

        private JObject BuildEcopathFleet(cCore core, int index, bool includeCatchByGroup)
        {
            var fleet = SafeGetIndexedProperty(core, "EcopathFleetInputs", index);
            var result = new JObject
            {
                ["index"] = index,
                ["name"] = SafeStringProperty(fleet, "Name")
            };

            if (includeCatchByGroup)
            {
                var catches = new JArray();
                var groupCount = SafeIntProperty(core, "nGroups");
                for (var groupIndex = 1; groupIndex <= groupCount; groupIndex++)
                {
                    var landings = SafeGetIndexedProperty(fleet, "Landings", groupIndex);
                    var discards = SafeGetIndexedProperty(fleet, "Discards", groupIndex);
                    if (!IsNonZero(landings) && !IsNonZero(discards))
                    {
                        continue;
                    }

                    var group = SafeGetIndexedProperty(core, "EcopathGroupInputs", groupIndex);
                    catches.Add(new JObject
                    {
                        ["group_index"] = groupIndex,
                        ["group_name"] = SafeStringProperty(group, "Name"),
                        ["landings"] = ToJToken(landings),
                        ["discards"] = ToJToken(discards)
                    });
                }
                result["catch_by_group"] = catches;
            }

            return result;
        }

        private JObject CreateEcopathGroup(JObject parameters)
        {
            var core = RequireCore();
            var name = RequiredString(parameters, "name");
            int groupType = OptionalInt(parameters, "group_type") ?? OptionalInt(parameters, "type") ?? 0; // 0=consumer, 1=producer, 2=detritus; group_type takes precedence over legacy type when both are provided
            int iGroup = SafeIntProperty(core, "nGroups") + 1;
            int iGroupID = 0;
            float sPP = (float)groupType;
            float sVBK = 0.0f; // von Bertalanffy K, default 0

            var method = core.GetType().GetMethod("AddGroup", new[] { typeof(string), typeof(float), typeof(float), typeof(int).MakeByRefType(), typeof(int).MakeByRefType() });
            if (method == null) throw new EwEMcpException("unsupported_operation", "AddGroup not available in this EwE build");

            // Capture active Ecosim scenario index before AddGroup triggers a batch lock release.
            // AddGroup internally calls ReleaseBatchLock(eBatchChangeLevelFlags.Ecopath) which
            // reloads Ecopath data via LoadModel but explicitly leaves Ecosim data stale
            // (Ecosim scenarios are only reloaded when the batch level is Ecosim or deeper).
            // We force-reload the Ecosim scenario afterwards to prevent array bounds errors.
            int activeEcosimIndex = SafeIntProperty(core, "ActiveEcosimScenarioIndex");

            object[] args = new object[] { name, sPP, sVBK, iGroup, iGroupID };
            bool ok = Convert.ToBoolean(method.Invoke(core, args));
            if (!ok) throw new EwEMcpException("create_failed", "Failed to create Ecopath group");

            int newGroupIndex = Convert.ToInt32(args[3]);
            int newGroupID = Convert.ToInt32(args[4]);

            // Force reload of the active Ecosim scenario to update its in-memory
            // data structures for the new group count. Without this, loading the
            // scenario later would read n+1 rows from EcosimScenarioGroup into
            // arrays sized for n groups, causing "Index was outside the bounds
            // of the array".
            if (activeEcosimIndex > 0)
            {
                var loadEcosimMethod = core.GetType().GetMethod("LoadEcosimScenario",
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public,
                    null, new[] { typeof(int) }, null);
                if (loadEcosimMethod != null)
                {
                    try
                    {
                        loadEcosimMethod.Invoke(core, new object[] { activeEcosimIndex });
                    }
                    catch (Exception ex)
                    {
                        EwEMcpFileLog.Warn($"CreateEcopathGroup: failed to reload Ecosim scenario {activeEcosimIndex}: {ex.Message}");
                    }
                }
            }

            // Set optional properties on the new group if provided.
            // After AddGroup the Ecopath data should be fresh, so SafeSetProperty
            // will write to the proper in-memory objects.
            var input = SafeGetIndexedProperty(core, "EcopathGroupInputs", newGroupIndex);
            if (input != null)
            {
                var biomassToken = parameters?["biomass"];
                if (biomassToken != null && biomassToken.Type != JTokenType.Null)
                {
                    SafeSetProperty(input, "BiomassAreaInput", Convert.ToSingle(biomassToken));
                }
                var pbToken = parameters?["pb_ratio"];
                if (pbToken != null && pbToken.Type != JTokenType.Null)
                {
                    SafeSetProperty(input, "PBInput", Convert.ToSingle(pbToken));
                }
                var qbToken = parameters?["qb_ratio"];
                if (qbToken != null && qbToken.Type != JTokenType.Null)
                {
                    SafeSetProperty(input, "QBInput", Convert.ToSingle(qbToken));
                }
                var eeToken = parameters?["ee"];
                if (eeToken != null && eeToken.Type != JTokenType.Null)
                {
                    SafeSetProperty(input, "EEInput", Convert.ToSingle(eeToken));
                }
            }

            // Mark the data source as dirty for the optional property sets above
            MarkDataSourceModified("ecopath_groups");

    return new JObject
    {
        ["created"] = true,
        ["group_index"] = newGroupIndex,
        ["name"] = name,
        ["type"] = GroupTypeFromInt(groupType),
        ["group_id"] = newGroupID,
        ["ecosim_reloaded"] = activeEcosimIndex > 0
    };
        }

        private static string GroupTypeFromInt(int type)
        {
            switch (type)
            {
                case 1: return "producer";
                case 2: return "detritus";
                default: return "consumer";
            }
        }

        private JObject CreateEcopathFleet(JObject parameters)
        {
            var core = RequireCore();
            var name = RequiredString(parameters, "name");
            int iFleet = SafeIntProperty(core, "nFleets") + 1;
            int iFleetID = 0;

            var method = core.GetType().GetMethod("AddFleet", new[] { typeof(string), typeof(int).MakeByRefType(), typeof(int).MakeByRefType() });
            if (method == null) throw new EwEMcpException("unsupported_operation", "AddFleet not available in this EwE build");

            object[] args = new object[] { name, iFleet, iFleetID };
            bool ok = Convert.ToBoolean(method.Invoke(core, args));
            if (!ok) throw new EwEMcpException("create_failed", "Failed to create Ecopath fleet");

            int newFleetIndex = Convert.ToInt32(args[1]);
            int newFleetID = Convert.ToInt32(args[2]);

    return new JObject
    {
        ["created"] = true,
        ["fleet_index"] = newFleetIndex,
        ["name"] = name,
        ["fleet_id"] = newFleetID,
        ["warning"] = "New fleet written to database but in-memory core counts (nFleets) are not immediately refreshed. The new fleet may not appear in list_ecopath_fleets or get_values until the model is saved and reloaded. Use save_model followed by load_model to refresh."
    };
    }

    private JObject ImportTimeSeries(JObject parameters)
    {
        var core = RequireCore();
        var csvData = RequiredString(parameters, "data");
        var importType = OptionalString(parameters, "import_type") ?? "biomass";
        var datasetName = OptionalString(parameters, "dataset_name") ?? "MCP Import";
        var delimiter = OptionalString(parameters, "delimiter") ?? ",";
        var decimalSeparator = OptionalString(parameters, "decimal_separator") ?? ".";
        var interval = OptionalString(parameters, "interval") ?? "annual";
        var enableOnLoad = OptionalBool(parameters, "enable_on_load");

        // Validate interval
        var intervalInt = interval.ToLowerInvariant() switch
        {
            "annual" => 0,    // eTSDataSetInterval.Annual
            "timestep" => 1,  // eTSDataSetInterval.TimeStep
            _ => throw new EwEMcpException("invalid_parameter", $"interval must be 'annual' or 'timestep', got '{interval}'")
        };

        // Validate import_type
        var validImportTypes = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "catch", "effort", "biomass", "forcing"
        };
        if (!validImportTypes.Contains(importType))
        {
            throw new EwEMcpException("invalid_parameter", $"import_type must be one of: catch, effort, biomass, forcing. Got '{importType}'");
        }

        // Verify Ecosim is loaded (time series require it)
        var sm = SafeGetProperty(core, "StateMonitor");
        bool hasEcosimLoaded = false;
        try { hasEcosimLoaded = SafeInvokeBool(sm, "HasEcosimLoaded"); } catch { }
        if (!hasEcosimLoaded)
        {
            throw new EwEMcpException("not_ready", "Ecosim must be loaded before importing time series. Load an Ecosim scenario first.");
        }

        // Write CSV data to a temporary file
        var tempDir = System.IO.Path.Combine(
            System.Environment.GetFolderPath(System.Environment.SpecialFolder.LocalApplicationData),
            "EwE_MCP"
        );
        System.IO.Directory.CreateDirectory(tempDir);
        var tempFile = System.IO.Path.Combine(tempDir, $"ts_import_{System.IO.Path.GetRandomFileName()}.csv");
        // Normalize line endings for Windows so EwE's StreamReader splits correctly.
        var normalizedCsv = csvData.Replace("\r\n", "\n").Replace("\n", "\r\n");
        // Use UTF‑8 without BOM; EwE's StreamReader handles it and non‑ASCII
        // series names are preserved.
        System.IO.File.WriteAllText(tempFile, normalizedCsv, new System.Text.UTF8Encoding(false));

        try
        {
            // Use reflection to create a cTimeSeriesCSVReader
            var readerFactoryType = core.GetType().Assembly.GetType("EwECore.cTimeSeriesReaderFactory");
            if (readerFactoryType == null)
            {
                throw new EwEMcpException("unsupported_operation", "cTimeSeriesReaderFactory not available in this EwE build");
            }

            // eTimeSeriesReaderTypes.CSV = 0
            var getReaderMethod = readerFactoryType.GetMethod("GetTimeSeriesReader",
                new[] { typeof(cCore), readerFactoryType.GetNestedType("eTimeSeriesReaderTypes") });
            if (getReaderMethod == null)
            {
                throw new EwEMcpException("unsupported_operation", "GetTimeSeriesReader method not available");
            }

            var csvEnumValue = Enum.ToObject(readerFactoryType.GetNestedType("eTimeSeriesReaderTypes"), 0);
            var reader = getReaderMethod.Invoke(null, new object[] { core, csvEnumValue });
            if (reader == null)
            {
                throw new EwEMcpException("import_failed", "Failed to create CSV reader");
            }

    // eTSDataSetInterval enum value - resolve before GetMethod so the signature matches
    var intervalEnumType = core.GetType().Assembly.GetType("EwECore.eTSDataSetInterval");
    object intervalEnumValue = intervalEnumType != null
        ? Enum.ToObject(intervalEnumType, intervalInt)
        : (object)intervalInt;
    Type intervalParamType = intervalEnumType ?? intervalInt.GetType();

    // Call Read(fileName, delimiter, decimalSeparator, interval)
    var readMethod = reader.GetType().GetMethod("Read",
        new[] { typeof(string), typeof(string), typeof(string), intervalParamType });
    if (readMethod == null)
    {
        throw new EwEMcpException("unsupported_operation", "CSV Read method not available in this EwE build");
    }

            bool readOk = false;
            string readErrorMessage = "";
            try
            {
                readOk = Convert.ToBoolean(readMethod.Invoke(reader, new object[] { tempFile, delimiter, decimalSeparator, intervalEnumValue }));
                if (!readOk)
                {
                    // Try to extract preview/error info from the reader
                    var previewProp = reader.GetType().GetProperty("Preview");
                    if (previewProp != null)
                    {
                        var preview = previewProp.GetValue(reader, null);
                        if (preview != null)
                        {
                            var colCount = SafeIntProperty(preview, "ColumnCount");
                            var rowCount = SafeIntProperty(preview, "RowCount");
                            // Try to get row errors
                            var rowErrorProp = preview.GetType().GetProperty("RowError", new[] { typeof(int) });
                            var errors = new System.Collections.Generic.List<string>();
                            if (rowErrorProp != null)
                            {
                                for (int r = 0; r < rowCount; r++)
                                {
                                    var err = rowErrorProp.GetValue(preview, new object[] { r })?.ToString();
                                    if (!string.IsNullOrEmpty(err))
                                        errors.Add($"Row {r}: {err}");
                                }
                            }
                            readErrorMessage = $"Reader preview: cols={colCount}, rows={rowCount}" + (errors.Count > 0 ? $", errors=[{string.Join("; ", errors)}]" : "");
                        }
                    }
                    // Also try to get the file contents for diagnostics
                    try
                    {
                        var fileBytes = System.IO.File.ReadAllBytes(tempFile);
                        readErrorMessage += $", file_size={fileBytes.Length}, first_bytes={BitConverter.ToString(fileBytes.Take(50).ToArray())}";
                    }
                    catch { }
                }
            }
            catch (Exception ex)
            {
                throw new EwEMcpException("import_failed", $"CSV read exception: {ex.InnerException?.Message ?? ex.Message}");
            }

            if (!readOk)
            {
                throw new EwEMcpException("import_failed", $"CSV file could not be read; check format and delimiters. {readErrorMessage}");
            }

            // Get FirstYear and NumPoints from the reader
            int firstYear = SafeIntProperty(reader, "FirstYear");
            int numPoints = SafeIntProperty(reader, "NumPoints");

            // Count time series from the reader (it implements ICollection)
            int tsCount = 0;
            var countProp = reader.GetType().GetProperty("Count");
            if (countProp != null)
            {
                tsCount = Convert.ToInt32(countProp.GetValue(reader, null));
            }

            if (tsCount == 0)
            {
                return new JObject
                {
                    ["status"] = "no_data",
                    ["rows_previewed"] = numPoints,
                    ["note"] = "CSV was parsed but no time series records were extracted. Check CSV format against EwE time series documentation."
                };
            }

            // Determine if we need to create a new dataset
            // Non-forcing series require a new dataset; forcing goes to active
            var categoryType = core.GetType().Assembly.GetType("EwECore.eTimeSeriesCategoryType");
            var timeSeriesClass = core.GetType().Assembly.GetType("EwECore.cTimeSeries");

            // Acquire batch lock for the import (matching dialog behavior)
            var batchLockType = core.GetType().Assembly.GetType("EwECore.cCore+eBatchLockType");
            var setBatchLockMethod = batchLockType != null
                ? core.GetType().GetMethod("SetBatchLock", new[] { batchLockType })
                : null;
            object restructureLock = batchLockType != null
                ? Enum.ToObject(batchLockType, 1) // eBatchLockType.Restructure = 1
                : (object)1;

            if (setBatchLockMethod != null)
            {
                bool lockOk = Convert.ToBoolean(setBatchLockMethod.Invoke(core, new object[] { restructureLock }));
                if (!lockOk)
                {
                    throw new EwEMcpException("import_failed", "Could not acquire batch lock for import");
                }
            }

            int iDataset = 0;
            int numTS = 0;
            int numForcing = 0;
            int numErrors = 0;
            bool createNewSet = true; // Always create a new dataset to get a valid ID
            var stepLog = new System.Collections.Generic.List<string>();

            try
            {
                // Check each time series to see if we need to create a new dataset
                for (int i = 0; i < tsCount; i++)
                {
                    var tsItem = SafeGetPropertyWithIndex(reader, "Item", i);
                    if (tsItem == null) continue;

                    var tsType = SafeGetProperty(tsItem, "TimeSeriesType");
                    if (tsType == null) continue;

                    // cTimeSeries.Category(tsType) tells if it's Group, Fleet, or Forcing
                    if (timeSeriesClass != null)
                    {
                        var categoryMethod = timeSeriesClass.GetMethod("Category",
                            new[] { tsType.GetType() });
                        if (categoryMethod != null)
                        {
                            var cat = categoryMethod.Invoke(null, new object[] { tsType });
                            if (cat != null)
                            {
                                int catValue = Convert.ToInt32(cat);
                                // Forcing = 0, Group = 1, Fleet = 2
                                if (catValue != 0) createNewSet = true;
                                stepLog.Add($"series[{i}]: cat={catValue}, needsNewSet={catValue != 0}");
                            }
                        }
                    }
                    break; // Only need to check the first one
                }

                if (createNewSet)
                {
                    // Create new dataset via AppendTimeSeriesDataset
                    var appendMethod = core.GetType().GetMethod("AppendTimeSeriesDataset",
                        new[] { typeof(string), typeof(string), typeof(string), typeof(string),
                                typeof(int), typeof(int), intervalEnumType ?? typeof(int),
                                typeof(int).MakeByRefType() });

                    if (appendMethod == null)
                    {
                        // Try without enum type
                        appendMethod = core.GetType().GetMethod("AppendTimeSeriesDataset");
                    }

                    if (appendMethod != null)
                    {
                        object[] appendArgs = new object[] { datasetName, "", "", "",
                            firstYear, numPoints, intervalEnumValue ?? intervalInt, iDataset };
                        bool appendOk = Convert.ToBoolean(appendMethod.Invoke(core, appendArgs));
                        iDataset = Convert.ToInt32(appendArgs[7]);

                        if (!appendOk)
                        {
                            stepLog.Add("AppendTimeSeriesDataset: FAILED (returned false)");
                            throw new EwEMcpException("import_failed", "Failed to create time series dataset");
                        }
                        stepLog.Add($"AppendTimeSeriesDataset: OK, iDataset={iDataset}");

                        // Refresh the time series data structures so DatasetDBID works
                        var initMethod = core.GetType().GetMethod("InitAndLoadEcosimTimeSeriesDatasets",
                            System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                        if (initMethod != null)
                        {
                            try { initMethod.Invoke(core, null); stepLog.Add("InitAndLoadEcosimTimeSeriesDatasets: OK"); }
                            catch (Exception iex) { stepLog.Add($"InitAndLoadEcosimTimeSeriesDatasets: {iex.Message}"); }
                        }
                    }
                    else
                    {
                        stepLog.Add("AppendTimeSeriesDataset: method not found");
                        throw new EwEMcpException("unsupported_operation", "AppendTimeSeriesDataset not available in this EwE build");
                    }
                }
                else
                {
                    // Use active dataset for forcing
                    iDataset = SafeIntProperty(core, "ActiveTimeSeriesDatasetIndex");
                    stepLog.Add($"Using active dataset (forcing), iDataset={iDataset}");
                }

                // Release batch lock before importing — AddTimeSeries acquires
                // its own lock internally and nesting would deadlock.
                var releaseBatchLock = core.GetType().GetMethod("ReleaseBatchLock",
                    new[] { batchLockType ?? typeof(int), typeof(bool) });
                if (releaseBatchLock != null && setBatchLockMethod != null)
                {
                    try { releaseBatchLock.Invoke(core, new object[] { restructureLock ?? 1, true }); } catch { }
                    setBatchLockMethod = null;
                }

                // Load the dataset before importing so ActiveDatasetIndex >= 0.
                // Use the actual iDataset (new or active) rather than a hardcoded index,
                // because dataset 1 may not exist or may not be the one we just created.
                var preloadMethod = core.GetType().GetMethod("LoadTimeSeries",
                    new[] { typeof(int), typeof(bool) });
                var preloadIdx = iDataset > 0 ? iDataset : 1;
                if (preloadMethod != null)
                {
                    try
                    {
                        preloadMethod.Invoke(core, new object[] { preloadIdx, false });
                        stepLog.Add($"Preload dataset {preloadIdx}: OK");
                    }
                    catch (Exception le) { stepLog.Add($"Preload dataset {preloadIdx}: {le.Message}"); }
                }

                // Use AddTimeSeries instead of ImportEcosimTimeSeries because
                // the latter's AddAsTimeSeries path fails when the EcosimTimeSeries
                // table is empty (MAX(TimeSeriesID) returns DBNull).
                var addTSMethod = core.GetType().GetMethod("AddTimeSeries",
                    new[] { typeof(string), typeof(int), typeof(int),
                            core.GetType().Assembly.GetType("EwECore.eTimeSeriesType"),
                            typeof(float), typeof(float[]), typeof(int).MakeByRefType() });
                stepLog.Add($"AddTimeSeries method: {(addTSMethod != null ? "found" : "NOT FOUND")}");

                // Reader has no public Item indexer — iterate via GetEnumerator.
                var readerEnum = reader.GetType().GetMethod("GetEnumerator")?.Invoke(reader, null);
                var moveNext = readerEnum?.GetType().GetMethod("MoveNext");
                var getCurrent = readerEnum?.GetType().GetProperty("Current")?.GetGetMethod();
                int importIdx = 0;
                while (moveNext != null && (bool)moveNext.Invoke(readerEnum, null))
                {
                    var tsItem = getCurrent?.Invoke(readerEnum, null);
                    importIdx++;
                    if (tsItem == null || addTSMethod == null) { numErrors++; continue; }

                    try
                    {
                        var name = SafeStringProperty(tsItem, "Name") ?? $"Imported_{importIdx}";
                        var pool = SafeIntProperty(tsItem, "DatPool");
                        var poolSec = SafeIntProperty(tsItem, "DatPoolSec");
                        var tsTypeVal = SafeGetProperty(tsItem, "TimeSeriesType");
                        var weight = Convert.ToSingle(SafeIntProperty(tsItem, "WtType"));
                        var nPts = SafeIntProperty(tsItem, "nPoints");

                        // Validate pool indices against current model dimensions to prevent
                        // IndexOutOfRangeException inside EwE's AddTimeSeries code.
                        // pool may be a group index (1..nGroups) or fleet index (1..nFleets),
                        // or 0 for forcing/global series. Use the larger dimension as upper bound.
                        var nGroups = SafeIntProperty(core, "nGroups");
                        var nFleets = SafeIntProperty(core, "nFleets");
                        var maxPool = Math.Max(nGroups, nFleets);
                        if (pool < 0 || pool > maxPool)
                        {
                            numErrors++;
                            stepLog.Add($"AddTimeSeries({importIdx}): 'DatPool' value {pool} is outside valid range 0..{maxPool} (nGroups={nGroups}, nFleets={nFleets})");
                            continue;
                        }
                        if (poolSec < 0 || poolSec > nGroups)
                        {
                            numErrors++;
                            stepLog.Add($"AddTimeSeries({importIdx}): 'DatPoolSec' value {poolSec} is outside valid group range 0..{nGroups}");
                            continue;
                        }

                        var values = new float[nPts];
                        for (int vi = 1; vi <= nPts; vi++)
                        {
                            var v = SafeGetPropertyWithIndex(tsItem, "ShapeData", vi);
                            values[vi - 1] = v != null ? Convert.ToSingle(v) : 0f;
                        }
                        int newDBID = 0;
                        object[] addArgs = new object[] { name, pool, poolSec, tsTypeVal, weight, values, newDBID };
                        bool added = Convert.ToBoolean(addTSMethod.Invoke(core, addArgs));
                        if (added)
                        {
                            numTS++;
                            stepLog.Add($"AddTimeSeries({importIdx}): OK, dbid={addArgs[6]}");
                        }
                        else
                        {
                            numErrors++;
                            stepLog.Add($"AddTimeSeries({importIdx}): returned false");
                        }
                    }
                    catch (Exception iex)
                    {
                        numErrors++;
                        stepLog.Add($"AddTimeSeries({importIdx}) threw: {iex.InnerException?.Message ?? iex.Message}");
                    }
                }
            }
            finally
            {
                // Release batch lock (may have been released early before import)
                if (setBatchLockMethod != null)
                {
                    var releaseMethod = core.GetType().GetMethod("ReleaseBatchLock",
                        new[] { batchLockType ?? typeof(int), typeof(bool) });
                    if (releaseMethod != null)
                    {
                        try { releaseMethod.Invoke(core, new object[] { restructureLock ?? 1, numErrors == 0 }); } catch { }
                    }
                }
            }

            // Load and optionally enable the time series
            bool loadOk = false;
            try
            {
                var loadMethod = core.GetType().GetMethod("LoadTimeSeries",
                    new[] { typeof(int), typeof(bool) });
                if (loadMethod != null && iDataset > 0)
                {
                    loadOk = Convert.ToBoolean(loadMethod.Invoke(core, new object[] { iDataset, enableOnLoad }));
                    stepLog.Add($"LoadTimeSeries({iDataset}): {(loadOk ? "OK" : "FAILED")}");
                }
                else
                {
                    stepLog.Add($"LoadTimeSeries: method=({loadMethod != null}), iDataset={iDataset} (must be >0)");
                }
            }
            catch (Exception lex)
            {
                stepLog.Add($"LoadTimeSeries threw: {lex.Message}");
            }

            return new JObject
            {
                ["status"] = numErrors == 0 ? "ok" : "partial",
                ["rows_imported"] = numTS + numForcing,
                ["time_series_count"] = numTS,
                ["forcing_count"] = numForcing,
                ["error_count"] = numErrors,
                ["dataset_index"] = iDataset,
                ["dataset_name"] = datasetName,
                ["import_type"] = importType,
                ["interval"] = interval,
                ["first_year"] = firstYear,
                ["num_points"] = numPoints,
                ["loaded"] = loadOk,
                ["total_in_file"] = tsCount,
                ["diagnostics"] = new JArray(stepLog.Select(s => new JValue(s)))
            };
        }
        finally
        {
            // Clean up temp file
            try { if (System.IO.File.Exists(tempFile)) System.IO.File.Delete(tempFile); } catch { }
            try { System.IO.Directory.Delete(tempDir, false); } catch { }
        }
    }

    private JObject ListEcosimScenarios()
        {
            var core = RequireCore();
            var scenarios = new JArray();
            var count = SafeIntProperty(core, "nEcosimScenarios");
            var activeIndex = SafeIntProperty(core, "ActiveEcosimScenarioIndex");
            for (var i = 1; i <= count; i++)
            {
                scenarios.Add(BuildEcosimScenario(core, i, activeIndex));
            }

            return new JObject
            {
                ["scenarios"] = scenarios,
                ["total_count"] = count,
                ["active_index"] = activeIndex > 0 ? JToken.FromObject(activeIndex) : JValue.CreateNull()
            };
        }

        private JObject GetEcosimScenario(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
            var count = SafeIntProperty(core, "nEcosimScenarios");
            if (index < 1 || index > count)
            {
                throw new EwEMcpException("index_out_of_range", $"Ecosim scenario index {index} is outside 1..{count}");
            }
            return BuildEcosimScenario(core, index, SafeIntProperty(core, "ActiveEcosimScenarioIndex"));
        }

        private JObject BuildEcosimScenario(cCore core, int index, int activeIndex)
        {
            var scenario = SafeGetIndexedProperty(core, "EcosimScenarios", index);
            var values = scenario == null ? new JObject() : ReadScalarValues(scenario, null, false, "ecosim_scenarios");
            return new JObject
            {
                ["index"] = index,
                ["name"] = SafeStringProperty(scenario, "Name"),
                ["description"] = SafeStringProperty(scenario, "Description"),
                ["author"] = SafeStringProperty(scenario, "Author"),
                ["contact"] = SafeStringProperty(scenario, "Contact"),
                ["dbid"] = SafeJTokenProperty(scenario, "DBID"),
                ["is_loaded"] = index == activeIndex,
                ["values"] = values
            };
        }

        private JObject LoadEcosimScenario(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
            var ok = core.LoadEcosimScenario(index);
            return new JObject
            {
                ["loaded"] = ok,
                ["index"] = index,
                ["scenario"] = ok ? BuildEcosimScenario(core, index, SafeIntProperty(core, "ActiveEcosimScenarioIndex")) : null
            };
        }

        private JObject LoadEcospaceScenario(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
            var sm = SafeGetProperty(core, "StateMonitor");
            if (sm != null && !SafeInvokeBool(sm, "HasEcosimLoaded"))
            {
                var loadEsim = core.GetType().GetMethod("LoadEcosimScenario", new[] { typeof(int) });
                if (loadEsim == null)
                    throw new EwEMcpException("unsupported_operation", "LoadEcosimScenario method not available");
                var loaded = Convert.ToBoolean(loadEsim.Invoke(core, new object[] { 1 }));
                if (!loaded)
                    throw new EwEMcpException("not_ready", "Auto-load of Ecosim scenario 1 failed. Load an Ecosim scenario manually before loading Ecospace.");
            }
            bool ok = RequireCore().LoadEcospaceScenario(index);
            return new JObject { ["loaded"] = ok, ["index"] = index };
        }

        private JObject LoadEcotracerScenario(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
            var sm = SafeGetProperty(core, "StateMonitor");
            if (sm != null && !SafeInvokeBool(sm, "HasEcosimLoaded"))
            {
                var loadEsim = core.GetType().GetMethod("LoadEcosimScenario", new[] { typeof(int) });
                if (loadEsim == null)
                    throw new EwEMcpException("unsupported_operation", "LoadEcosimScenario method not available");
                var loaded = Convert.ToBoolean(loadEsim.Invoke(core, new object[] { 1 }));
                if (!loaded)
                    throw new EwEMcpException("not_ready", "Auto-load of Ecosim scenario 1 failed. Load an Ecosim scenario manually before loading Ecotracer.");
            }
            bool ok = RequireCore().LoadEcotracerScenario(index);
            return new JObject { ["loaded"] = ok, ["index"] = index };
        }

        // ── Ecospace Handlers ───────────────────────────────────────────

        private JObject ListEcospaceScenarios()
        {
            var core = RequireCore();
            int count = SafeIntProperty(core, "nEcospaceScenarios");
            var scenarios = new JArray();
            for (var i = 1; i <= count; i++)
            {
                var scenario = SafeGetIndexedProperty(core, "EcospaceScenarios", i);
                if (scenario == null) continue;
                scenarios.Add(new JObject
                {
                    ["index"] = i,
                    ["name"] = SafeStringProperty(scenario, "Name"),
                    ["description"] = SafeStringProperty(scenario, "Description"),
                });
            }
            return new JObject
            {
                ["scenarios"] = scenarios,
                ["total_count"] = count,
                ["active_index"] = SafeIntProperty(core, "ActiveEcospaceScenarioIndex"),
            };
        }

        private JObject GetEcospaceScenario(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var scenario = SafeGetIndexedProperty(core, "EcospaceScenarios", index);
            if (scenario == null) throw new EwEMcpException("index_out_of_range", $"Ecospace scenario index {index} out of range");
            return new JObject
            {
                ["index"] = index,
                ["name"] = SafeStringProperty(scenario, "Name"),
                ["description"] = SafeStringProperty(scenario, "Description"),
                ["author"] = SafeStringProperty(scenario, "Author"),
                ["contact"] = SafeStringProperty(scenario, "Contact"),
                ["last_saved"] = SafeStringProperty(scenario, "LastSaved"),
                ["is_loaded"] = SafeIntProperty(core, "ActiveEcospaceScenarioIndex") == index,
            };
        }

        private JObject GetEcospaceRunState()
        {
            var core = RequireCore();
            var sm = SafeGetProperty(core, "StateMonitor");
            return new JObject
            {
                ["has_ecospace_loaded"] = sm != null && SafeInvokeBool(sm, "HasEcospaceLoaded"),
                ["has_ecospace_initialized"] = sm != null && SafeInvokeBool(sm, "HasEcospaceInitialized"),
                ["is_ecospace_running"] = sm != null && SafeInvokeBool(sm, "IsEcospaceRunning"),
                ["has_ecospace_ran"] = sm != null && SafeInvokeBool(sm, "HasEcospaceRan"),
                ["is_ecospace_modified"] = sm != null && SafeInvokeBool(sm, "IsEcospaceModified"),
                ["is_ecospace_paused"] = SafeBoolProperty(core, "EcospacePaused"),
                ["active_scenario_index"] = SafeIntProperty(core, "ActiveEcospaceScenarioIndex"),
            };
        }

        private JObject RunEcospace()
        {
            var core = RequireCore();
            var delegateType = typeof(cCore.EcoSpaceInterfaceDelegate);
            var method = core.GetType().GetMethod("RunEcospace", new[] { delegateType.MakeByRefType(), typeof(bool) });
            object[] invokeArgs = new object[] { null, true };
            if (method == null)
            {
                var candidates = core.GetType().GetMethods(BindingFlags.Instance | BindingFlags.Public)
                    .Where(m => m.Name == "RunEcospace")
                    .ToList();
                if (candidates.Count == 0) throw new EwEMcpException("core_unavailable", "RunEcospace not available");
                var candidate = candidates.FirstOrDefault(m => {
                    var ps = m.GetParameters();
                    return ps.Length == 2 && ps[0].ParameterType.HasElementType && ps[0].ParameterType.GetElementType() == delegateType && ps[1].ParameterType == typeof(bool);
                }) ?? candidates.FirstOrDefault(m => {
                    var ps = m.GetParameters();
                    return ps.Length >= 1 && ps.Length <= 3 && ps.All(p => p.IsOptional);
                });
                if (candidate == null) throw new EwEMcpException("core_unavailable", $"RunEcospace not available (found {candidates.Count} overloads, none compatible)");
                method = candidate;
                var ps = method.GetParameters();
                invokeArgs = new object[ps.Length];
                for (var i = 0; i < ps.Length; i++)
                {
    if (ps[i].ParameterType == typeof(bool)) invokeArgs[i] = true;
    else if (ps[i].IsOptional) invokeArgs[i] = Type.Missing;
    else invokeArgs[i] = null;
                }
            }
            bool ok = Convert.ToBoolean(method.Invoke(core, invokeArgs));
            return new JObject { ["started"] = ok };
        }

        private JObject StopEcospace()
        {
            var core = RequireCore();
            var method = core.GetType().GetMethod("StopEcospace", Type.EmptyTypes);
            if (method == null) throw new EwEMcpException("core_unavailable", "StopEcospace not available");
            method.Invoke(core, null);
            return new JObject { ["stopped"] = true };
        }

        private JObject ListEcospaceGroupInputs()
        {
            var core = RequireCore();
            int nGroups = SafeIntProperty(core, "nGroups");
            var items = new JArray();
            for (var i = 1; i <= nGroups; i++)
            {
                var group = SafeGetIndexedProperty(core, "EcospaceGroupInputs", i);
                if (group == null) continue;
                items.Add(new JObject { ["index"] = i, ["name"] = SafeStringProperty(group, "Name") });
            }
            return new JObject { ["items"] = items, ["total_count"] = nGroups };
        }

        private JObject GetEcospaceGroupInput(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var group = SafeGetIndexedProperty(core, "EcospaceGroupInputs", index);
            if (group == null) throw new EwEMcpException("index_out_of_range", $"Ecospace group input index {index} out of range. Load an Ecospace scenario first.");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(group, "Name"), ["values"] = ReadScalarValues(group, null, false, "ecospace_group_inputs") };
        }

        private JObject ListEcospaceFleetInputs()
        {
            var core = RequireCore();
            int nFleets = SafeIntProperty(core, "nFleets");
            var items = new JArray();
            for (var i = 1; i <= nFleets; i++)
            {
                var fleet = SafeGetIndexedProperty(core, "EcospaceFleetInputs", i);
                if (fleet == null) continue;
                items.Add(new JObject { ["index"] = i, ["name"] = SafeStringProperty(fleet, "Name") });
            }
            return new JObject { ["items"] = items, ["total_count"] = nFleets };
        }

        private JObject GetEcospaceFleetInput(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var fleet = SafeGetIndexedProperty(core, "EcospaceFleetInputs", index);
            if (fleet == null) throw new EwEMcpException("index_out_of_range", $"Ecospace fleet input index {index} out of range. Load an Ecospace scenario first.");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(fleet, "Name"), ["values"] = ReadScalarValues(fleet, null, false, "ecospace_fleet_inputs") };
        }

        private JObject GetEcospaceGroupResults(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var groupCount = SafeIntProperty(core, "nGroups");
            if (index < 1 || index > groupCount) throw new EwEMcpException("index_out_of_range", $"Ecospace group index {index} outside 1..{groupCount}");
            var output = SafeGetIndexedProperty(core, "EcospaceGroupOutput", index);
            if (output == null) throw new EwEMcpException("entity_unavailable", $"Ecospace group output {index} unavailable. Run Ecospace first.");
            var maxTime = SafeIntProperty(core, "nEcospaceTimeSteps");
            if (maxTime < 1) throw new EwEMcpException("ecospace_results_unavailable", "Ecospace has no result timesteps.");
            var start = OptionalInt(parameters, "start_time") ?? 1;
            var end = OptionalInt(parameters, "end_time") ?? Math.Min(maxTime, start + 119);
            if (start < 1 || start > maxTime || end < start || end > maxTime)
                throw new EwEMcpException("time_range_out_of_range", $"Ecospace time range {start}..{end} outside 1..{maxTime}");
            var variables = OptionalStringArray(parameters, "variables") ?? new[] { "biomass", "relative_biomass" };
            var series = new JObject();
            foreach (var variable in variables)
            {
                var arr = new JArray();
                for (var t = start; t <= end; t++)
                {
                    object val = null;
                    switch ((variable ?? string.Empty).Trim().ToLowerInvariant())
                    {
                        case "biomass": val = SafeGetIndexedProperty(output, "Biomass", t); break;
                        case "relative_biomass": val = SafeGetIndexedProperty(output, "RelativeBiomass", t); break;
                        default:
                            throw new EwEMcpException("unknown_ecospace_result_variable", $"Unknown Ecospace result variable: {variable}");
                    }
                    arr.Add(new JObject { ["time"] = t, ["value"] = ToJToken(val) });
                }
                series[variable] = arr;
            }
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(output, "Name"), ["time_start"] = start, ["time_end"] = end, ["time_steps"] = maxTime, ["series"] = series };
        }

        private JObject GetEcospaceFleetResults(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var fleetCount = SafeIntProperty(core, "nFleets");
            if (index < 1 || index > fleetCount) throw new EwEMcpException("index_out_of_range", $"Ecospace fleet index {index} outside 1..{fleetCount}");
            var output = SafeGetIndexedProperty(core, "EcospaceFleetOutput", index);
            if (output == null) throw new EwEMcpException("entity_unavailable", $"Ecospace fleet output {index} unavailable. Run Ecospace first.");
            var maxTime = SafeIntProperty(core, "nEcospaceTimeSteps");
            if (maxTime < 1) throw new EwEMcpException("ecospace_results_unavailable", "Ecospace has no result timesteps.");
            var start = OptionalInt(parameters, "start_time") ?? 1;
            var end = OptionalInt(parameters, "end_time") ?? Math.Min(maxTime, start + 119);
            if (start < 1 || start > maxTime || end < start || end > maxTime)
                throw new EwEMcpException("time_range_out_of_range", $"Ecospace time range {start}..{end} outside 1..{maxTime}");
            var variables = OptionalStringArray(parameters, "variables") ?? new[] { "catch_biomass", "value" };
            var series = new JObject();
            foreach (var variable in variables)
            {
                var arr = new JArray();
                for (var t = start; t <= end; t++)
                {
                    object val = null;
                    switch ((variable ?? string.Empty).Trim().ToLowerInvariant())
                    {
                        case "catch_biomass": val = SafeGetIndexedProperty(output, "CatchBiomass", t); break;
                        case "value": val = SafeGetIndexedProperty(output, "Value", t); break;
                        default:
                            throw new EwEMcpException("unknown_ecospace_result_variable", $"Unknown Ecospace result variable: {variable}");
                    }
                    arr.Add(new JObject { ["time"] = t, ["value"] = ToJToken(val) });
                }
                series[variable] = arr;
            }
            return new JObject { ["index"] = index, ["time_start"] = start, ["time_end"] = end, ["time_steps"] = maxTime, ["series"] = series };
        }

        private JObject GetEcospaceHabitat(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var habitat = SafeGetIndexedProperty(core, "EcospaceHabitats", index);
            if (habitat == null) throw new EwEMcpException("index_out_of_range", $"Ecospace habitat index {index} out of range");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(habitat, "Name"), ["values"] = ReadScalarValues(habitat, null, false, "ecospace_habitats") };
        }

        private JObject GetEcospaceMpa(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var mpa = SafeGetIndexedProperty(core, "EcospaceMPAs", index);
            if (mpa == null) throw new EwEMcpException("index_out_of_range", $"Ecospace MPA index {index} out of range");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(mpa, "Name"), ["values"] = ReadScalarValues(mpa, null, false, "ecospace_mpas") };
        }

        private JObject GetEcospaceBasemap()
        {
            var core = RequireCore();
            var basemap = SafeGetProperty(core, "EcospaceBasemap");
            if (basemap == null) throw new EwEMcpException("core_unavailable", "Ecospace basemap not available. Load an Ecospace scenario first.");
            int rows = SafeIntProperty(basemap, "InRow");
            int cols = SafeIntProperty(basemap, "InCol");
            var depthLayer = SafeGetProperty(basemap, "LayerDepth");
            var grid = new JArray();
            if (depthLayer != null)
            {
                for (int r = 1; r <= rows; r++)
                {
                    var rowArr = new JArray();
                    for (int c = 1; c <= cols; c++)
                    {
                        var cellObj = SafeGetIndexedProperty(depthLayer, "Cell", r, c);
                        if (cellObj != null)
                        {
                            try { rowArr.Add(Convert.ToSingle(cellObj)); }
                            catch { rowArr.Add(-1f); }
                        }
                        else { rowArr.Add(-1f); }
                    }
                    grid.Add(rowArr);
                }
            }
            return new JObject
            {
                ["rows"] = rows,
                ["cols"] = cols,
                ["grid"] = grid,
                ["cell_length"] = SafeJTokenProperty(basemap, "CellLength"),
                ["cell_size"] = SafeJTokenProperty(basemap, "CellSize"),
                ["latitude"] = SafeJTokenProperty(basemap, "Latitude"),
                ["longitude"] = SafeJTokenProperty(basemap, "Longitude"),
            };
        }

        private JObject GetEcospaceModelParameters()
        {
            var core = RequireCore();
            var parameters = SafeGetProperty(core, "EcospaceModelParameters");
            if (parameters == null) throw new EwEMcpException("core_unavailable", "Ecospace model parameters not available. Load an Ecospace scenario first.");
            return ReadScalarValues(parameters, null, false, "ecospace_model_parameters");
        }

        private JObject CreateEcospaceScenario(JObject parameters)
        {
            var core = RequireCore();
            var name = RequiredString(parameters, "name");
            var description = parameters?["description"]?.ToString() ?? string.Empty;
            var author = parameters?["author"]?.ToString() ?? string.Empty;
            var contact = parameters?["contact"]?.ToString() ?? string.Empty;
            int rows = RequiredInt(parameters, "rows");
            int cols = RequiredInt(parameters, "cols");
            float lat = RequiredFloat(parameters, "latitude");
            float lon = RequiredFloat(parameters, "longitude");
            float cellLength = RequiredFloat(parameters, "cell_length");
            var method = core.GetType().GetMethod("NewEcospaceScenario", new[] { typeof(string), typeof(string), typeof(string), typeof(string), typeof(int), typeof(int), typeof(float), typeof(float), typeof(float) });
            if (method == null) throw new EwEMcpException("unsupported_operation", "NewEcospaceScenario not available in this EwE build");
            // Pre-checks matching cCore.NewEcospaceScenario to give a clear error
            var ds = SafeGetProperty(core, "DataSource");
            var canSaveMethod = core.GetType().GetMethod("CanSave", new[] { typeof(bool) });
            bool canSave = canSaveMethod != null && Convert.ToBoolean(canSaveMethod.Invoke(core, new object[] { false }));
            var dsType = ds?.GetType().FullName ?? "(null)";
            var hasIface = ds != null && ds.GetType().GetInterface("IEcospaceDatasource") != null;
            var sm = SafeGetProperty(core, "StateMonitor");
            var hasLoaded = sm != null && SafeInvokeBool(sm, "HasEcopathLoaded");
            var hasEcosimLoaded = sm != null && SafeInvokeBool(sm, "HasEcosimLoaded");
            if (!canSave)
                throw new EwEMcpException("create_failed", $"Cannot create Ecospace scenario: CanSave=false");
            if (!hasIface)
                throw new EwEMcpException("create_failed", $"Cannot create Ecospace scenario: DataSource {dsType} does not implement IEcospaceDatasource");
            if (!hasLoaded)
                throw new EwEMcpException("create_failed", "Cannot create Ecospace scenario: Ecopath has not loaded");
            // LoadEcospaceScenario inside NewEcospaceScenario requires an Ecosim
            // scenario to be loaded.  Auto-load Ecosim scenario 1 if none is active.
            if (!hasEcosimLoaded)
            {
                var loadEcosimMethod = core.GetType().GetMethod("LoadEcosimScenario", new[] { typeof(int) });
                if (loadEcosimMethod == null)
                    throw new EwEMcpException("create_failed", "Cannot auto-load Ecosim scenario: LoadEcosimScenario not available");
                var esOk = Convert.ToBoolean(loadEcosimMethod.Invoke(core, new object[] { 1 }));
                if (!esOk)
                    throw new EwEMcpException("create_failed", "Cannot auto-load Ecosim scenario 1");
            }
            try
            {
                bool ok = Convert.ToBoolean(method.Invoke(core, new object[] { name, description, author, contact, rows, cols, lat, lon, cellLength }));
                if (!ok)
                    throw new EwEMcpException("create_failed", "NewEcospaceScenario returned false");
            }
            catch (TargetInvocationException tie)
            {
                throw new EwEMcpException("create_failed",
                    $"NewEcospaceScenario threw: {tie.InnerException?.GetType().Name}: {tie.InnerException?.Message ?? "(no message)"}");
            }
            return new JObject { ["created"] = true, ["index"] = SafeIntProperty(core, "ActiveEcospaceScenarioIndex") };
        }

        private float RequiredFloat(JObject parameters, string name)
        {
            var token = parameters?[name];
            if (token == null) throw new EwEMcpException("missing_parameter", $"Missing required float parameter: {name}");
            try { return Convert.ToSingle(token); }
            catch { throw new EwEMcpException("invalid_parameter", $"Parameter {name} must be a number"); }
        }

        private JObject CopyEcospaceScenario(JObject parameters)
        {
            var core = RequireCore();
            var name = RequiredString(parameters, "name");
            var description = parameters?["description"]?.ToString() ?? string.Empty;
            if (SafeIntProperty(core, "ActiveEcospaceScenarioIndex") < 1)
                throw new EwEMcpException("ecospace_scenario_not_loaded", "Load an Ecospace scenario before copying it.");
            var method = core.GetType().GetMethod("SaveEcospaceScenarioAs", new[] { typeof(string), typeof(string) });
            if (method == null) throw new EwEMcpException("unsupported_operation", "SaveEcospaceScenarioAs not available in this EwE build");
            bool ok = Convert.ToBoolean(method.Invoke(core, new object[] { name, description }));
            if (!ok) throw new EwEMcpException("copy_failed", "Failed to copy Ecospace scenario");
            return new JObject { ["copied"] = true, ["index"] = SafeIntProperty(core, "ActiveEcospaceScenarioIndex") };
        }

        private JObject DeleteEcospaceScenario(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var activeIndex = SafeIntProperty(core, "ActiveEcospaceScenarioIndex");
            if (index == activeIndex)
                throw new EwEMcpException("cannot_delete_active_scenario", "Close or load a different Ecospace scenario before deleting this one.");
            var method = core.GetType().GetMethod("RemoveEcospaceScenario", new[] { typeof(int) });
            if (method == null) throw new EwEMcpException("unsupported_operation", "Ecospace scenario deletion not supported by this EwE build");
            bool ok = Convert.ToBoolean(method.Invoke(core, new object[] { index }));
            return new JObject { ["deleted"] = ok, ["index"] = index };
        }

        private JObject SaveEcospaceScenario()
        {
            var core = RequireCore();
            var method = core.GetType().GetMethod("SaveEcospaceScenario", new Type[0]);
            if (method == null) throw new EwEMcpException("unsupported_operation", "SaveEcospaceScenario not available in this EwE build");
            bool ok = Convert.ToBoolean(method.Invoke(core, null));
            return new JObject { ["saved"] = ok };
        }

        // ── Ecospace Spatial / MPA / Region Handlers ──────────────────

    private JObject GetEcospaceMapSummary()
    {
        var core = RequireCore();
        var basemap = SafeGetProperty(core, "EcospaceBasemap");
        if (basemap == null) throw new EwEMcpException("core_unavailable", "Ecospace basemap not available. Load an Ecospace scenario first.");

        int rows = SafeIntProperty(basemap, "InRow");
        int cols = SafeIntProperty(basemap, "InCol");

        // Count land and water cells from the depth layer
        int landCells = 0;
        int waterCells = 0;
        var depthLayer = SafeGetProperty(basemap, "LayerDepth");
        if (depthLayer != null)
        {
            for (int r = 1; r <= rows; r++)
            {
                for (int c = 1; c <= cols; c++)
                {
                    // Cell is an indexed property with (row, col); use two-index accessor
                    var cellObj = SafeGetIndexedProperty(depthLayer, "Cell", r, c);
                    if (cellObj != null)
                    {
                        try
                        {
                            float depth = Convert.ToSingle(cellObj);
                            if (depth > 0)
                                waterCells++;
                            else
                                landCells++;
                        }
                        catch
                        {
                            landCells++;
                        }
                    }
                    else
                    {
                        landCells++;
                    }
                }
            }
        }

        return new JObject
        {
            ["rows"] = rows,
            ["cols"] = cols,
            ["land_cells"] = landCells,
            ["water_cells"] = waterCells,
            ["n_mpa"] = SafeIntProperty(core, "nMPAs"),
            ["n_habitats"] = SafeIntProperty(core, "nHabitats"),
            ["n_regions"] = SafeIntProperty(core, "nRegions"),
        };
    }

    private JObject CreateEcospaceMpa(JObject parameters)
    {
        var core = RequireCore();
        var name = RequiredString(parameters, "name");
        bool isActive = OptionalBool(parameters, "is_active");

        if (SafeIntProperty(core, "ActiveEcospaceScenarioIndex") < 1)
            throw new EwEMcpException("ecospace_scenario_not_loaded", "Load an Ecospace scenario before creating an MPA.");

        int newIndex = SafeIntProperty(core, "nMPAs") + 1;
        int dbid = 0;
        var months = new bool[13]; // 1-based; all false = MPA closed (active) all year
        if (isActive)
        {
            // is_active=true means MPA is enforced; the months array
            // indicates when the MPA is OPEN for fishing, so leave all false
        }

        var method = core.GetType().GetMethod("AddEcospaceMPA", new[] { typeof(string), typeof(int), typeof(bool[]), typeof(int).MakeByRefType() });
        if (method == null) throw new EwEMcpException("unsupported_operation", "AddEcospaceMPA not available in this EwE build");

        bool ok = false;
        try
        {
            object[] args = new object[] { name, newIndex, months, dbid };
            ok = Convert.ToBoolean(method.Invoke(core, args));
            dbid = (args[3] != null && args[3] != DBNull.Value) ? Convert.ToInt32(args[3]) : 0;
        }
        catch (Exception ex)
        {
            var inner = ex.InnerException ?? ex;
            throw new EwEMcpException("create_failed", $"Failed to create MPA: {inner.Message}");
        }

        if (!ok) throw new EwEMcpException("create_failed", "Failed to create Ecospace MPA");
        return new JObject { ["created"] = true, ["index"] = newIndex, ["name"] = name, ["dbid"] = dbid };
    }

    private JObject SetEcospaceMpaCells(JObject parameters)
    {
        var core = RequireCore();
        int mpaIndex = RequiredInt(parameters, "mpa_index");

        var cellsToken = parameters?["cells"];
        if (cellsToken == null || cellsToken.Type != JTokenType.Array)
            throw new EwEMcpException("missing_parameter", "Missing required parameter: cells (array of [row, col] pairs)");

        var basemap = SafeGetProperty(core, "EcospaceBasemap");
        if (basemap == null) throw new EwEMcpException("core_unavailable", "Ecospace basemap not available. Load an Ecospace scenario first.");

        // Access basemap.LayerMPA(mpaIndex) to get the MPA layer
        var mpaLayer = SafeGetPropertyWithIndex(basemap, "LayerMPA", mpaIndex);
        if (mpaLayer == null) throw new EwEMcpException("index_out_of_range", $"MPA layer index {mpaIndex} out of range");

        int cellsSet = 0;
        int cellsSkipped = 0;
        int cellsFailed = 0;
        int maxRows = SafeIntProperty(basemap, "InRow");
        int maxCols = SafeIntProperty(basemap, "InCol");

        foreach (var cellPair in cellsToken)
        {
            if (cellPair is JArray arr && arr.Count >= 2)
            {
                int row = arr[0].Value<int>();
                int col = arr[1].Value<int>();
                if (row < 1 || row > maxRows || col < 1 || col > maxCols)
                {
                    cellsSkipped++;
                    continue;
                }
                try
                {
                    // Set Cell(row, col) = 1 on the MPA layer (1 = MPA active)
                    mpaLayer.GetType().InvokeMember(
                        "Cell",
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.SetProperty,
                        null,
                        mpaLayer,
                        new object[] { 1, row, col }
                    );
                    cellsSet++;
                }
                catch
                {
                    cellsFailed++;
                }
            }
        }

        return new JObject { ["set"] = true, ["mpa_index"] = mpaIndex, ["cells_set"] = cellsSet, ["cells_skipped"] = cellsSkipped, ["cells_failed"] = cellsFailed };
    }

    private JObject CreateRegionsFromMPAs(JObject parameters)
    {
        var core = RequireCore();
        var basemap = SafeGetProperty(core, "EcospaceBasemap");
        if (basemap == null) throw new EwEMcpException("core_unavailable", "Ecospace basemap not available. Load an Ecospace scenario first.");

        int nMPAs = SafeIntProperty(core, "nMPAs");
        int? regionCountOverride = OptionalInt(parameters, "region_count");
        int regionCount = regionCountOverride ?? nMPAs;

        if (regionCount < 1)
            throw new EwEMcpException("invalid_parameter", "region_count must be at least 1");

        // Set nRegions on the Ecospace model parameters
        var modelParams = SafeGetProperty(core, "EcospaceModelParameters");
        if (modelParams != null)
        {
            SafeSetProperty(modelParams, "nRegions", regionCount);
        }

        // Get the region layer
        var regionLayer = SafeGetProperty(basemap, "LayerRegion");
        int rows = SafeIntProperty(basemap, "InRow");
        int cols = SafeIntProperty(basemap, "InCol");

        int cellsAssigned = 0;

        if (regionLayer != null)
        {
            // Copy MPA cell assignments to the region layer
            for (int mpaIdx = 1; mpaIdx <= nMPAs; mpaIdx++)
            {
                var mpaLayer = SafeGetPropertyWithIndex(basemap, "LayerMPA", mpaIdx);
                if (mpaLayer == null) continue;

                for (int r = 1; r <= rows; r++)
                {
                    for (int c = 1; c <= cols; c++)
                    {
                        try
                        {
                            var mpaCellVal = SafeGetIndexedProperty(mpaLayer, "Cell", r, c);
                            if (mpaCellVal != null && Convert.ToInt32(mpaCellVal) > 0)
                            {
                                // Assign this cell to the MPA's region number
                                regionLayer.GetType().InvokeMember(
                                    "Cell",
                                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.SetProperty,
                                    null,
                                    regionLayer,
                                    new object[] { mpaIdx, r, c }
                                );
                                cellsAssigned++;
                            }
                        }
                        catch
                        {
                            // Skip cells that fail
                        }
                    }
                }
            }
        }

        return new JObject
        {
            ["regions_created"] = true,
            ["region_count"] = regionCount,
            ["cells_assigned"] = cellsAssigned,
            ["mpas_processed"] = nMPAs,
        };
    }

    // ── Ecotracer Handlers ──────────────────────────────────────────

        private JObject ListEcotracerScenarios()
        {
            var core = RequireCore();
            int count = SafeIntProperty(core, "nEcotracerScenarios");
            var scenarios = new JArray();
            for (var i = 1; i <= count; i++)
            {
                var scenario = SafeGetIndexedProperty(core, "EcotracerScenarios", i);
                if (scenario == null) continue;
                scenarios.Add(new JObject
                {
                    ["index"] = i,
                    ["name"] = SafeStringProperty(scenario, "Name"),
                    ["description"] = SafeStringProperty(scenario, "Description"),
                });
            }
            return new JObject
            {
                ["scenarios"] = scenarios,
                ["total_count"] = count,
                ["active_index"] = SafeIntProperty(core, "ActiveEcotracerScenarioIndex"),
            };
        }

        private JObject GetEcotracerScenario(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var scenario = SafeGetIndexedProperty(core, "EcotracerScenarios", index);
            if (scenario == null) throw new EwEMcpException("index_out_of_range", $"Ecotracer scenario index {index} out of range");
            return new JObject
            {
                ["index"] = index,
                ["name"] = SafeStringProperty(scenario, "Name"),
                ["description"] = SafeStringProperty(scenario, "Description"),
                ["author"] = SafeStringProperty(scenario, "Author"),
                ["contact"] = SafeStringProperty(scenario, "Contact"),
                ["last_saved"] = SafeStringProperty(scenario, "LastSaved"),
                ["is_loaded"] = SafeIntProperty(core, "ActiveEcotracerScenarioIndex") == index,
            };
        }

        private JObject GetEcotracerRunState()
        {
            var core = RequireCore();
            var sm = SafeGetProperty(core, "StateMonitor");
            return new JObject
            {
                ["has_ecotracer_loaded"] = sm != null && SafeInvokeBool(sm, "HasEcotracerLoaded"),
                ["has_ecotracer_ran_for_ecosim"] = sm != null && SafeInvokeBool(sm, "HasEcotracerRanForEcosim"),
                ["has_ecotracer_ran_for_ecospace"] = sm != null && SafeInvokeBool(sm, "HasEcotracerRanForEcospace"),
                ["is_ecotracer_modified"] = sm != null && SafeInvokeBool(sm, "IsEcotracerModified"),
                ["active_scenario_index"] = SafeIntProperty(core, "ActiveEcotracerScenarioIndex"),
            };
        }

        private JObject GetEcotracerModelParameters()
        {
            var core = RequireCore();
            var parameters = SafeGetProperty(core, "EcotracerModelParameters");
            if (parameters == null) throw new EwEMcpException("core_unavailable", "Ecotracer model parameters not available");
            return ReadScalarValues(parameters, null, false, "ecotracer_model_parameters");
        }

        private JObject ListEcotracerGroupInputs()
        {
            var core = RequireCore();
            int nGroups = SafeIntProperty(core, "nGroups");
            var items = new JArray();
            for (var i = 1; i <= nGroups; i++)
            {
                var group = SafeGetIndexedProperty(core, "EcotracerGroupInputs", i);
                if (group == null) continue;
                items.Add(new JObject { ["index"] = i, ["name"] = SafeStringProperty(group, "Name") });
            }
            return new JObject { ["items"] = items, ["total_count"] = nGroups };
        }

        private JObject GetEcotracerGroupInput(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var group = SafeGetIndexedProperty(core, "EcotracerGroupInputs", index);
            if (group == null) throw new EwEMcpException("index_out_of_range", $"Ecotracer group input index {index} out of range");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(group, "Name"), ["values"] = ReadScalarValues(group, null, false, "ecotracer_group_inputs") };
        }

        private JObject GetEcotracerGroupResults(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var results = SafeGetProperty(core, "EcotracerGroupResults");
            if (results == null) throw new EwEMcpException("core_unavailable", "Ecotracer group results not available. Run Ecosim/Ecospace with Ecotracer enabled first.");
            int nTimeSteps = SafeIntProperty(core, "nEcosimTimeSteps");
            if (nTimeSteps < 1) nTimeSteps = SafeIntProperty(core, "nEcospaceTimeSteps");
        var start = OptionalInt(parameters, "start_time") ?? 1;
        if (nTimeSteps < 1) throw new EwEMcpException("ecotracer_results_unavailable", "No time steps available for Ecotracer results.");
        var end = OptionalInt(parameters, "end_time") ?? Math.Min(nTimeSteps, start + 119);
            if (start < 1 || start > nTimeSteps || end < start || end > nTimeSteps)
                throw new EwEMcpException("time_range_out_of_range", $"Time range {start}..{end} outside 1..{nTimeSteps}");
            var variables = OptionalStringArray(parameters, "variables") ?? new[] { "concentration", "conbio" };
            var series = new JObject();
            foreach (var variable in variables)
            {
                var arr = new JArray();
                for (var t = start; t <= end; t++)
                {
                    object val = null;
                    switch ((variable ?? string.Empty).Trim().ToLowerInvariant())
                    {
                        case "concentration": val = SafeGetIndexedProperty(results, "Concentration", index, t); break;
                        case "conbio": val = SafeGetIndexedProperty(results, "ConBio", index, t); break;
                        default:
                            throw new EwEMcpException("unknown_ecotracer_result_variable", $"Unknown Ecotracer result variable: {variable}");
                    }
                    arr.Add(new JObject { ["time"] = t, ["value"] = ToJToken(val) });
                }
                series[variable] = arr;
            }
            return new JObject { ["index"] = index, ["time_start"] = start, ["time_end"] = end, ["series"] = series };
        }

        private JObject CreateEcotracerScenario(JObject parameters)
        {
            var core = RequireCore();
            var name = RequiredString(parameters, "name");
            var description = parameters?["description"]?.ToString() ?? string.Empty;
            var author = parameters?["author"]?.ToString() ?? string.Empty;
            var contact = parameters?["contact"]?.ToString() ?? string.Empty;
            var method = core.GetType().GetMethod("NewEcotracerScenario", new[] { typeof(string), typeof(string), typeof(string), typeof(string) });
            if (method == null) throw new EwEMcpException("unsupported_operation", "NewEcotracerScenario not available in this EwE build");
            bool ok = Convert.ToBoolean(method.Invoke(core, new object[] { name, description, author, contact }));
            if (!ok) throw new EwEMcpException("create_failed", "Failed to create Ecotracer scenario");
            return new JObject { ["created"] = true, ["index"] = SafeIntProperty(core, "ActiveEcotracerScenarioIndex") };
        }

        private JObject CopyEcotracerScenario(JObject parameters)
        {
            var core = RequireCore();
            var name = RequiredString(parameters, "name");
            var description = parameters?["description"]?.ToString() ?? string.Empty;
            if (SafeIntProperty(core, "ActiveEcotracerScenarioIndex") < 1)
                throw new EwEMcpException("ecotracer_scenario_not_loaded", "Load an Ecotracer scenario before copying it.");
            var method = core.GetType().GetMethod("SaveEcotracerScenarioAs", new[] { typeof(string), typeof(string) });
            if (method == null) throw new EwEMcpException("unsupported_operation", "SaveEcotracerScenarioAs not available in this EwE build");
            bool ok = Convert.ToBoolean(method.Invoke(core, new object[] { name, description }));
            if (!ok) throw new EwEMcpException("copy_failed", "Failed to copy Ecotracer scenario");
            return new JObject { ["copied"] = true, ["index"] = SafeIntProperty(core, "ActiveEcotracerScenarioIndex") };
        }

        private JObject DeleteEcotracerScenario(JObject parameters)
        {
            var core = RequireCore();
            int index = RequiredInt(parameters, "index");
            var activeIndex = SafeIntProperty(core, "ActiveEcotracerScenarioIndex");
            if (index == activeIndex)
                throw new EwEMcpException("cannot_delete_active_scenario", "Close or load a different Ecotracer scenario before deleting this one.");
            var method = core.GetType().GetMethod("RemoveEcotracerScenario", new[] { typeof(int) });
            if (method == null) throw new EwEMcpException("unsupported_operation", "Ecotracer scenario deletion not supported by this EwE build");
            bool ok = Convert.ToBoolean(method.Invoke(core, new object[] { index }));
            return new JObject { ["deleted"] = ok, ["index"] = index };
        }

        private JObject SaveEcotracerScenario()
        {
            var core = RequireCore();
            var method = core.GetType().GetMethod("SaveEcotracerScenario", new Type[0]);
            if (method == null) throw new EwEMcpException("unsupported_operation", "SaveEcotracerScenario not available in this EwE build");
            bool ok = Convert.ToBoolean(method.Invoke(core, null));
            return new JObject { ["saved"] = ok };
        }

        private JObject CloseEcotracerScenario()
        {
            var core = RequireCore();
            core.GetType().GetMethod("CloseEcotracerScenario")?.Invoke(core, null);
            return new JObject { ["closed"] = true };
        }

        // ── MSE Handlers ────────────────────────────────────────────────

        private object SafeGetSubProperty(object parent, string propertyName)
        {
            if (parent == null) return null;
            try
            {
                var prop = parent.GetType().GetProperty(propertyName, BindingFlags.Instance | BindingFlags.Public);
                return prop?.GetValue(parent);
            }
            catch { return null; }
        }

        private JObject RequireMseManager()
        {
            var manager = SafeGetProperty(RequireCore(), "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            return JObject.FromObject(new { mse_available = true });
        }

        private JObject GetMseParameters()
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            var modelParams = SafeGetProperty(manager, "ModelParameters");
            if (modelParams == null) throw new EwEMcpException("core_unavailable", "MSE model parameters not available");
            return ReadScalarValues(modelParams, null, false, "mse_parameters");
        }

        private JObject ListMseGroupInputs()
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            int nGroups = SafeIntProperty(core, "nGroups");
            var items = new JArray();
            for (var i = 1; i <= nGroups; i++)
            {
                var inputCollection = SafeGetProperty(manager, "GroupInputs");
                var group = SafeGetIndexedProperty(inputCollection, "Item", i);
                if (group == null) continue;
                items.Add(new JObject { ["index"] = i, ["name"] = SafeStringProperty(group, "Name") });
            }
            return new JObject { ["items"] = items, ["total_count"] = nGroups };
        }

        private JObject GetMseGroupInput(JObject parameters)
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            int index = RequiredInt(parameters, "index");
            var inputCollection = SafeGetProperty(manager, "GroupInputs");
            var group = SafeGetIndexedProperty(inputCollection, "Item", index);
            if (group == null) throw new EwEMcpException("index_out_of_range", $"MSE group input index {index} out of range");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(group, "Name"), ["values"] = ReadScalarValues(group, null, false, "mse_group_inputs") };
        }

        private JObject ListMseFleetInputs()
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            int nFleets = SafeIntProperty(core, "nFleets");
            var items = new JArray();
            for (var i = 1; i <= nFleets; i++)
            {
                var inputCollection = SafeGetProperty(manager, "EcopathFleetInputs");
                var fleet = SafeGetIndexedProperty(inputCollection, "Item", i);
                if (fleet == null) continue;
                items.Add(new JObject { ["index"] = i, ["name"] = SafeStringProperty(fleet, "Name") });
            }
            return new JObject { ["items"] = items, ["total_count"] = nFleets };
        }

        private JObject GetMseFleetInput(JObject parameters)
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            int index = RequiredInt(parameters, "index");
            var inputCollection = SafeGetProperty(manager, "EcopathFleetInputs");
            var fleet = SafeGetIndexedProperty(inputCollection, "Item", index);
            if (fleet == null) throw new EwEMcpException("index_out_of_range", $"MSE fleet input index {index} out of range");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(fleet, "Name"), ["values"] = ReadScalarValues(fleet, null, false, "mse_fleet_inputs") };
        }

        private JObject GetMseRunState()
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            var sm = SafeGetProperty(core, "StateMonitor");
            string searchMode = "not_in_search";
            if (sm != null)
            {
                var mode = SafeInvoke(sm, "SearchMode");
                if (mode != null) searchMode = mode.ToString();
            }
            return new JObject
            {
                ["mse_available"] = manager != null,
                ["is_running"] = manager != null && SafeBoolProperty(manager, "IsRunning"),
                ["is_connected"] = manager != null && SafeBoolProperty(manager, "isConnected"),
                ["search_mode"] = searchMode,
            };
        }

        private JObject RunMse()
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            var method = manager.GetType().GetMethod("Run", new Type[0]);
            if (method == null) throw new EwEMcpException("core_unavailable", "MSE Run method not available");
            bool ok = Convert.ToBoolean(method.Invoke(manager, null));
            return new JObject { ["started"] = ok };
        }

        private JObject StopMse()
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            try
            {
                manager.GetType().GetMethod("StopRun")?.Invoke(manager, new object[] { -1 });
            }
            catch { }
            return new JObject { ["stopped"] = true };
        }

        private JObject GetMseGroupResults(JObject parameters)
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            int index = RequiredInt(parameters, "index");
            var outputCollection = SafeGetProperty(manager, "GroupOutputs");
            if (outputCollection == null) throw new EwEMcpException("core_unavailable", "MSE group outputs not available. Run MSE first.");
            var groupOutput = SafeGetIndexedProperty(outputCollection, "Item", index);
            if (groupOutput == null) throw new EwEMcpException("index_out_of_range", $"MSE group output index {index} out of range");
            return new JObject { ["index"] = index, ["values"] = ReadScalarValues(groupOutput, null, false, "mse_group_outputs") };
        }

        private JObject GetMseFleetResults(JObject parameters)
        {
            var core = RequireCore();
            var manager = SafeGetProperty(core, "MSEManager");
            if (manager == null) throw new EwEMcpException("core_unavailable", "MSE not available");
            int index = RequiredInt(parameters, "index");
            var outputCollection = SafeGetProperty(manager, "FleetOutputs");
            if (outputCollection == null) throw new EwEMcpException("core_unavailable", "MSE fleet outputs not available. Run MSE first.");
            var fleetOutput = SafeGetIndexedProperty(outputCollection, "Item", index);
            if (fleetOutput == null) throw new EwEMcpException("index_out_of_range", $"MSE fleet output index {index} out of range");
            return new JObject { ["index"] = index, ["values"] = ReadScalarValues(fleetOutput, null, false, "mse_fleet_outputs") };
        }

        // ── Monte Carlo Handlers ────────────────────────────────────────

        private JObject GetMonteCarloParameters()
        {
            var core = RequireCore();
            var mc = SafeGetProperty(core, "EcosimMonteCarlo");
            if (mc == null) throw new EwEMcpException("core_unavailable", "Monte Carlo not available");
            return new JObject
            {
                ["n_trials"] = SafeIntProperty(mc, "nTrials"),
                ["is_best_fit"] = SafeBoolProperty(mc, "IsBestFit"),
                ["retain_fits"] = SafeBoolProperty(mc, "RetainFits"),
                ["include_fpenalty"] = SafeBoolProperty(mc, "IncludeFpenalty"),
                ["is_running"] = SafeBoolProperty(mc, "IsRunning"),
            };
        }

        private JObject ListMonteCarloGroups()
        {
            var core = RequireCore();
            var mc = SafeGetProperty(core, "EcosimMonteCarlo");
            if (mc == null) throw new EwEMcpException("core_unavailable", "Monte Carlo not available");
            int nGroups = SafeIntProperty(core, "nGroups");
            var items = new JArray();
            var groups = SafeGetProperty(mc, "Groups");
            for (var i = 1; i <= nGroups; i++)
            {
                var group = SafeGetIndexedProperty(groups, "Item", i);
                if (group == null) continue;
                items.Add(new JObject { ["index"] = i, ["name"] = SafeStringProperty(group, "Name") });
            }
            return new JObject { ["items"] = items, ["total_count"] = nGroups };
        }

        private JObject GetMonteCarloGroup(JObject parameters)
        {
            var core = RequireCore();
            var mc = SafeGetProperty(core, "EcosimMonteCarlo");
            if (mc == null) throw new EwEMcpException("core_unavailable", "Monte Carlo not available");
            int index = RequiredInt(parameters, "index");
            var groups = SafeGetProperty(mc, "Groups");
            var group = SafeGetIndexedProperty(groups, "Item", index);
            if (group == null) throw new EwEMcpException("index_out_of_range", $"Monte Carlo group index {index} out of range");
            return new JObject { ["index"] = index, ["name"] = SafeStringProperty(group, "Name"), ["values"] = ReadScalarValues(group, null, false, "monte_carlo_groups") };
        }

        private JObject GetMonteCarloRunState()
        {
            var core = RequireCore();
            var mc = SafeGetProperty(core, "EcosimMonteCarlo");
            var sm = SafeGetProperty(core, "StateMonitor");
            string searchMode = "not_in_search";
            if (sm != null)
            {
                var mode = SafeInvoke(sm, "SearchMode");
                if (mode != null) searchMode = mode.ToString();
            }
            return new JObject
            {
                ["monte_carlo_available"] = mc != null,
                ["is_running"] = mc != null && SafeBoolProperty(mc, "IsRunning"),
                ["is_best_fit"] = mc != null && SafeBoolProperty(mc, "IsBestFit"),
                ["search_mode"] = searchMode,
            };
        }

        private JObject RunMonteCarlo()
        {
            var core = RequireCore();
            var mc = SafeGetProperty(core, "EcosimMonteCarlo");
            if (mc == null) throw new EwEMcpException("core_unavailable", "Monte Carlo not available");
            var method = mc.GetType().GetMethod("Run", new Type[0]);
            if (method == null) throw new EwEMcpException("core_unavailable", "Monte Carlo Run method not available");
            bool ok = Convert.ToBoolean(method.Invoke(mc, null));
            return new JObject { ["started"] = ok };
        }

        private JObject StopMonteCarlo()
        {
            var core = RequireCore();
            var mc = SafeGetProperty(core, "EcosimMonteCarlo");
            if (mc == null) throw new EwEMcpException("core_unavailable", "Monte Carlo not available");
            var innerModel = SafeGetProperty(mc, "m_mc");
            if (innerModel != null)
            {
                innerModel.GetType().GetProperty("StopTrial")?.SetValue(innerModel, true);
            }
            else
            {
                mc.GetType().GetMethod("StopRun", new[] { typeof(int) })?.Invoke(mc, new object[] { -1 });
            }
            return new JObject { ["stopped"] = true };
        }

        // ── Plugin Diagnostics ───────────────────────────────────────────

        private JObject GetInstalledPlugins()
        {
            var core = RequireCore();
            var pluginManager = SafeGetProperty(core, "PluginManager");
            if (pluginManager == null) throw new EwEMcpException("core_unavailable", "Plugin manager not available");
            var plugins = new JArray();
            try
            {
                var getPluginsMethod = pluginManager.GetType().GetMethod("GetPlugins");
                if (getPluginsMethod != null)
                {
                    var iPluginType = getPluginsMethod.ReturnType.IsGenericType
                        ? getPluginsMethod.ReturnType.GetGenericArguments().FirstOrDefault()
                        : null;
                    if (iPluginType == null)
                    {
                        var asm = pluginManager.GetType().Assembly;
                        iPluginType = asm.GetTypes().FirstOrDefault(t => t.Name == "IPlugin");
                    }
                    if (iPluginType != null)
                    {
                        var pluginCollection = getPluginsMethod.Invoke(pluginManager, new object[] { iPluginType });
                        if (pluginCollection is System.Collections.IEnumerable enumerable)
                        {
                            foreach (var plugin in enumerable)
                            {
                                plugins.Add(new JObject
                                {
                                    ["name"] = SafeStringProperty(plugin, "Name"),
                                    ["description"] = SafeStringProperty(plugin, "Description"),
                                    ["author"] = SafeStringProperty(plugin, "Author"),
                                    ["contact"] = SafeStringProperty(plugin, "Contact"),
                                    ["type"] = plugin.GetType().Name,
                                });
                            }
                        }
                    }
                }
            }
            catch { }
            return new JObject { ["plugins"] = plugins, ["total_count"] = plugins.Count };
        }

        private JObject CreateEcosimScenario(JObject parameters)
        {
            var name = RequiredString(parameters, "name");
            var description = parameters?["description"]?.ToString() ?? string.Empty;
            var author = parameters?["author"]?.ToString() ?? string.Empty;
            var contact = parameters?["contact"]?.ToString() ?? string.Empty;
            var core = RequireCore();
            var ok = core.NewEcosimScenario(name, description, author, contact);
            return new JObject
            {
                ["created"] = ok,
                ["name"] = name,
                ["active_index"] = SafeIntProperty(core, "ActiveEcosimScenarioIndex")
            };
        }

        private JObject CopyEcosimScenario(JObject parameters)
        {
            var name = RequiredString(parameters, "name");
            var description = parameters?["description"]?.ToString() ?? string.Empty;
            var core = RequireCore();

            // Support optional source_index: load that scenario first before copying.
            // If omitted, the active scenario is copied.
            var sourceIndex = OptionalInt(parameters, "source_index");
            var originalActive = SafeIntProperty(core, "ActiveEcosimScenarioIndex");

            if (sourceIndex.HasValue)
            {
                var nScenarios = SafeIntProperty(core, "nEcosimScenarios");
                if (sourceIndex.Value < 1 || sourceIndex.Value > nScenarios)
                {
                    throw new EwEMcpException("index_out_of_range",
                        $"source_index {sourceIndex.Value} is outside 1..{nScenarios}");
                }
                // Load the source scenario so it becomes active, then copy it
                if (!core.LoadEcosimScenario(sourceIndex.Value))
                {
                    // Restore original active scenario before failing
                    if (originalActive > 0)
                    {
                        try { core.LoadEcosimScenario(originalActive); } catch { }
                    }
                    throw new EwEMcpException("scenario_load_failed",
                        $"Failed to load Ecosim scenario {sourceIndex.Value} for copying.");
                }
            }
            else if (originalActive < 1)
            {
                throw new EwEMcpException("ecosim_scenario_not_loaded",
                    "Load an Ecosim scenario before copying it, or provide a source_index.");
            }

            var copyOk = false;
            int newActiveIndex = 0;
            try
            {
                copyOk = core.SaveEcosimScenarioAs(name, description);
                newActiveIndex = SafeIntProperty(core, "ActiveEcosimScenarioIndex");

                if (!copyOk)
                {
                    // SaveEcosimScenarioAs failed but may have left state inconsistent.
                    // Try to reload the original active scenario.
                    if (originalActive > 0)
                    {
                        try { core.LoadEcosimScenario(originalActive); } catch { }
                    }
                    throw new EwEMcpException("ecosim_copy_failed",
                        $"Failed to copy Ecosim scenario as '{name}'. The EwE core returned an error. " +
                        "This may occur if the model state is inconsistent (e.g., " +
                        "groups/fleets were added after the source scenario was created). " +
                        "Verify that all scenarios are compatible with the current model structure.");
                }
            }
            catch (EwEMcpException)
            {
                throw;
            }
            catch (Exception ex)
            {
                // Wrap any unexpected exception from the EwE core
                if (originalActive > 0)
                {
                    try { core.LoadEcosimScenario(originalActive); } catch { }
                }
                throw new EwEMcpException("ecosim_copy_failed",
                    $"Ecosim scenario copy threw an exception: {ex.Message}");
            }

            return new JObject
            {
                ["copied"] = true,
                ["name"] = name,
                ["active_index"] = newActiveIndex,
                ["source_index_used"] = sourceIndex ?? originalActive
            };
        }

        private JObject DeleteEcosimScenario(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
            var nScenarios = SafeIntProperty(core, "nEcosimScenarios");
            if (index < 1 || index > nScenarios)
            {
                throw new EwEMcpException("index_out_of_range", $"Ecosim scenario index {index} is outside 1..{nScenarios}");
            }
            var activeIndex = SafeIntProperty(core, "ActiveEcosimScenarioIndex");
            if (index == activeIndex)
            {
                throw new EwEMcpException("cannot_delete_active_scenario", "Close or load a different Ecosim scenario before deleting this one.");
            }
            var ok = core.RemoveEcosimScenario(index);
            return new JObject { ["deleted"] = ok, ["index"] = index };
        }

        private JObject SaveEcosimScenario()
        {
            var core = RequireCore();
            var ok = core.SaveEcosimScenario();
            return new JObject
            {
                ["saved"] = ok,
                ["active_index"] = SafeIntProperty(core, "ActiveEcosimScenarioIndex")
            };
        }

        private JObject GetEcosimRunState()
        {
            var core = RequireCore();
            var sm = SafeGetProperty(core, "StateMonitor");
            var activeIndex = SafeIntProperty(core, "ActiveEcosimScenarioIndex");
            var isRunning = SafeInvokeBool(sm, "IsEcosimRunning");
            var hasRan = SafeInvokeBool(sm, "HasEcosimRan");
            var hasInitialized = SafeInvokeBool(sm, "HasEcosimInitialized");
            var execState = SafeInvokeToString(sm, "CoreExecutionState") ?? string.Empty;

            // Compute derived run_state
            string runState;
            string ecoSimDetail;

            if (isRunning)
            {
                runState = "running";
                ecoSimDetail = "Ecosim is running";
            }
            else if (hasRan)
            {
                runState = "completed";
                ecoSimDetail = "Ecosim has completed";
            }
            else if (execState.IndexOf("Error", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                runState = "error";
                ecoSimDetail = "Ecosim encountered an error";
            }
            else if (execState.IndexOf("Stopped", StringComparison.OrdinalIgnoreCase) >= 0
                     || execState.IndexOf("Stop", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                runState = "stopped";
                ecoSimDetail = "Ecosim was stopped";
            }
            else if (hasInitialized)
            {
                runState = "initialized";
                ecoSimDetail = "Ecosim is initialized but not yet run";
            }
            else
            {
                runState = "idle";
                ecoSimDetail = "Ecosim scenario loaded, not initialized";
            }

            // Improve detail when we have the execution state
            if (!string.IsNullOrWhiteSpace(execState) && !isRunning)
            {
                ecoSimDetail = execState;
            }

            var state = new JObject
            {
                ["run_state"] = runState,
                ["ecosim_detail"] = ecoSimDetail,
                ["core_execution_state"] = execState,
                ["is_busy"] = SafeInvokeBool(sm, "IsBusy"),
                ["has_ecopath_loaded"] = SafeInvokeBool(sm, "HasEcopathLoaded"),
                ["has_ecopath_ran"] = SafeInvokeBool(sm, "HasEcopathRan"),
                ["has_ecosim_loaded"] = SafeInvokeBool(sm, "HasEcosimLoaded"),
                ["has_ecosim_initialized"] = hasInitialized,
                ["is_ecosim_running"] = isRunning,
                ["has_ecosim_ran"] = hasRan,
                ["active_scenario_index"] = activeIndex > 0 ? JToken.FromObject(activeIndex) : JValue.CreateNull(),
                ["scenario_count"] = SafeIntProperty(core, "nEcosimScenarios"),
                ["years"] = SafeIntProperty(core, "nEcosimYears"),
                ["time_steps"] = SafeIntProperty(core, "nEcosimTimeSteps"),
                ["first_year"] = SafeInvokeInt(core, "EcosimFirstYear"),
                ["queried_at"] = DateTime.UtcNow.ToString("o")
            };
            if (activeIndex > 0)
            {
                state["active_scenario"] = BuildEcosimScenario(core, activeIndex, activeIndex);
            }
            return state;
        }

        private JObject ListEcosimGroupInputs()
        {
            var core = RequireCore();
            var groups = new JArray();
            var count = SafeIntProperty(core, "nGroups");
            for (var i = 1; i <= count; i++)
            {
                groups.Add(BuildEcosimGroupInput(core, i));
            }
            return new JObject { ["groups"] = groups, ["total_count"] = count };
        }

        private JObject GetEcosimGroupInput(JObject parameters)
        {
            return BuildEcosimGroupInput(RequireCore(), RequiredInt(parameters, "index"));
        }

        private JObject BuildEcosimGroupInput(cCore core, int index)
        {
            var count = SafeIntProperty(core, "nGroups");
            if (index < 1 || index > count)
            {
                throw new EwEMcpException("index_out_of_range", $"Ecosim group index {index} is outside 1..{count}");
            }
            var input = SafeGetIndexedProperty(core, "EcosimGroupInputs", index);
            if (input == null)
            {
                throw new EwEMcpException("entity_unavailable", $"Ecosim group input {index} is unavailable. Load an Ecosim scenario first.");
            }
            return new JObject
            {
                ["index"] = index,
                ["name"] = SafeStringProperty(input, "Name"),
                ["values"] = ReadScalarValues(input, null, false, "ecosim_group_inputs")
            };
        }

        private JObject ListEcosimFleetInputs()
        {
            var core = RequireCore();
            var fleets = new JArray();
            var count = SafeIntProperty(core, "nFleets");
            for (var i = 1; i <= count; i++)
            {
                fleets.Add(BuildEcosimFleetInput(core, i));
            }
            return new JObject { ["fleets"] = fleets, ["total_count"] = count };
        }

        private JObject GetEcosimFleetInput(JObject parameters)
        {
            return BuildEcosimFleetInput(RequireCore(), RequiredInt(parameters, "index"));
        }

        private JObject BuildEcosimFleetInput(cCore core, int index)
        {
            var count = SafeIntProperty(core, "nFleets");
            if (index < 1 || index > count)
            {
                throw new EwEMcpException("index_out_of_range", $"Ecosim fleet index {index} is outside 1..{count}");
            }
            var input = SafeGetIndexedProperty(core, "EcosimFleetInputs", index);
            if (input == null)
            {
                throw new EwEMcpException("entity_unavailable", $"Ecosim fleet input {index} is unavailable. Load an Ecosim scenario first.");
            }
        return new JObject
        {
            ["index"] = index,
            ["values"] = ReadScalarValues(input, null, false, "ecosim_fleet_inputs")
        };
    }

    #region Ecosim Forcing Shapes

    private JObject ListEcosimForcingShapes()
    {
        var core = RequireCore();
        var manager = SafeGetProperty(core, "ForcingShapeManager");
        if (manager == null)
        {
            throw new EwEMcpException("entity_unavailable", "ForcingShapeManager is unavailable. Load an Ecosim scenario first.");
        }
        var count = SafeIntProperty(manager, "Count");
        var shapes = new JArray();
        for (var i = 0; i < count; i++)
        {
            var shape = SafeGetPropertyWithIndex(manager, "Item", i);
            if (shape != null)
            {
                shapes.Add(BuildForcingShapeSummary(shape, i));
            }
        }
        return new JObject { ["shapes"] = shapes, ["total_count"] = count };
    }

    private JObject GetEcosimForcingShape(JObject parameters)
    {
        var core = RequireCore();
        var index = RequiredInt(parameters, "index");
        return BuildEcosimForcingShape(core, index);
    }

    private JObject BuildEcosimForcingShape(cCore core, int index)
    {
        var manager = SafeGetProperty(core, "ForcingShapeManager");
        if (manager == null)
        {
            throw new EwEMcpException("entity_unavailable", "ForcingShapeManager is unavailable. Load an Ecosim scenario first.");
        }
        var count = SafeIntProperty(manager, "Count");
        if (index < 1 || index > count)
        {
            throw new EwEMcpException("index_out_of_range", $"Forcing shape index {index} is outside 1..{count}");
        }
        // Shape manager uses zero-based Item index
        var shape = SafeGetPropertyWithIndex(manager, "Item", index - 1);
        if (shape == null)
        {
            throw new EwEMcpException("entity_unavailable", $"Forcing shape {index} is unavailable.");
        }
        var nPoints = SafeIntProperty(shape, "nPoints");
        var data = new JArray();
        for (var i = 1; i <= nPoints; i++)
        {
            var val = SafeGetPropertyWithIndex(shape, "ShapeData", i);
            data.Add(val != null ? Convert.ToDouble(val) : 0.0);
        }
        return new JObject
        {
            ["index"] = index,
            ["name"] = SafeStringProperty(shape, "Name"),
            ["n_points"] = nPoints,
            ["shape_data"] = data,
            ["is_seasonal"] = SafeBoolProperty(shape, "IsSeasonal"),
            ["shape_function_type"] = SafeIntProperty(shape, "ShapeFunctionType"),
        };
    }

    private JObject SetEcosimForcingShape(JObject parameters)
    {
        var core = RequireCore();
        var index = RequiredInt(parameters, "index");
        var shapeData = parameters["shape_data"] as JArray
            ?? throw new EwEMcpException("missing_parameter", "Missing required parameter: shape_data");

        var manager = SafeGetProperty(core, "ForcingShapeManager");
        if (manager == null)
        {
            throw new EwEMcpException("entity_unavailable", "ForcingShapeManager is unavailable. Load an Ecosim scenario first.");
        }
        var count = SafeIntProperty(manager, "Count");
        if (index < 1 || index > count)
        {
            throw new EwEMcpException("index_out_of_range", $"Forcing shape index {index} is outside 1..{count}");
        }
        // Shape manager uses zero-based Item index
        var shape = SafeGetPropertyWithIndex(manager, "Item", index - 1);
        if (shape == null)
        {
            throw new EwEMcpException("entity_unavailable", $"Forcing shape {index} is unavailable.");
        }

        var nPoints = SafeIntProperty(shape, "nPoints");
        if (shapeData.Count != nPoints)
        {
            throw new EwEMcpException("invalid_shape_data", $"shape_data length {shapeData.Count} does not match nPoints {nPoints}");
        }

            // Set each data point using the indexed ShapeData(int) property.
            // cShapeData has two ShapeData overloads: ShapeData() As Single() and
            // ShapeData(iPoint As Integer) As Single.  We must resolve the indexer
            // explicitly to avoid an AmbiguousMatchException.
            var indexedProp = shape.GetType().GetProperty(
                "ShapeData", BindingFlags.Instance | BindingFlags.Public, null,
                typeof(float), new Type[] { typeof(int) }, null);
            if (indexedProp == null)
            {
                throw new EwEMcpException("set_failed", "Could not resolve indexed ShapeData property on shape object");
            }
            for (var i = 0; i < shapeData.Count; i++)
            {
                var pointIndex = i + 1; // ShapeData is one-based
                var value = (float)shapeData[i];
                try
                {
                    indexedProp.SetValue(shape, value, new object[] { pointIndex });
                }
                catch (Exception ex)
                {
                    throw new EwEMcpException("set_failed", $"Failed to set ShapeData({pointIndex}): {ex.Message}");
                }
            }

            // Mark the Ecosim data source as dirty so the shape changes are persisted
            MarkDataSourceModified("ecosim_scenarios");

        return new JObject
        {
            ["index"] = index,
            ["name"] = SafeStringProperty(shape, "Name"),
            ["n_points"] = nPoints,
            ["result"] = "updated",
        };
    }

    private static JObject BuildForcingShapeSummary(object shape, int zeroBasedIndex)
    {
        return new JObject
        {
            ["index"] = zeroBasedIndex + 1,
            ["name"] = SafeStringProperty(shape, "Name"),
            ["n_points"] = SafeIntProperty(shape, "nPoints"),
            ["is_seasonal"] = SafeBoolProperty(shape, "IsSeasonal"),
            ["shape_function_type"] = SafeIntProperty(shape, "ShapeFunctionType"),
        };
    }

    #endregion

    #region Ecosim PP Forcing

    private JObject ListEcosimPPForcing()
    {
        var core = RequireCore();
        var nGroups = SafeIntProperty(core, "nGroups");
        var groups = new JArray();
        var miManager = SafeGetProperty(core, "MediatedInteractionManager");

        for (var i = 1; i <= nGroups; i++)
        {
            var entry = BuildPPForcingEntry(core, miManager, i, summaryOnly: true);
            if (entry != null)
            {
                groups.Add(entry);
            }
        }
        return new JObject { ["groups"] = groups, ["total_count"] = nGroups };
    }

    private JObject GetEcosimPPForcing(JObject parameters)
    {
        var core = RequireCore();
        var index = RequiredInt(parameters, "index");
        var nGroups = SafeIntProperty(core, "nGroups");
        if (index < 1 || index > nGroups)
        {
            throw new EwEMcpException("index_out_of_range", $"PP forcing group index {index} is outside 1..{nGroups}");
        }
        var miManager = SafeGetProperty(core, "MediatedInteractionManager");
        var entry = BuildPPForcingEntry(core, miManager, index, summaryOnly: false);
        if (entry == null)
        {
            throw new EwEMcpException("entity_unavailable", $"PP forcing data for group {index} is unavailable.");
        }
        return entry;
    }

    private JObject BuildPPForcingEntry(cCore core, object miManager, int groupIndex, bool summaryOnly)
    {
        // Get the group name from EcosimGroupInputs
        var groupName = "";
        var groupInput = SafeGetIndexedProperty(core, "EcosimGroupInputs", groupIndex);
        if (groupInput != null)
        {
            groupName = SafeStringProperty(groupInput, "Name");
        }

        // Try to get the PredPreyInteraction for this group (group x group diagonal)
        object interaction = null;
        if (miManager != null)
        {
            interaction = SafeInvokeWithArgs(miManager, "PredPreyInteraction", new object[] { groupIndex, groupIndex });
            // If no PredPrey interaction, try GroupInteraction (mortality forcing)
            if (interaction == null)
            {
                interaction = SafeGetPropertyWithIndex(miManager, "GroupInteraction", groupIndex);
            }
        }

        // Extract applied shape info
        var appliedShapes = new JArray();
        if (interaction != null)
        {
            var maxShapes = SafeIntProperty(interaction, "MaxNumShapes");
            var nApplied = SafeIntProperty(interaction, "nAppliedShapes");
            for (var si = 1; si <= maxShapes; si++)
            {
                // Call getShape(iItem, out shape, out functionType) via reflection
                var shapeAndType = InvokeGetShape(interaction, si);
                if (shapeAndType != null)
                {
                    appliedShapes.Add(shapeAndType);
                }
            }
        }

        var result = new JObject
        {
            ["index"] = groupIndex,
            ["name"] = groupName,
            ["n_applied_shapes"] = appliedShapes.Count,
        };

        if (!summaryOnly)
        {
            result["applied_shapes"] = appliedShapes;
        }

        return result;
    }

    private JObject InvokeGetShape(object interaction, int itemIndex)
    {
        try
        {
            var method = interaction.GetType().GetMethod("getShape", BindingFlags.Instance | BindingFlags.Public);
            if (method == null) return null;

            var args = new object[] { itemIndex, null, null };
            var result = method.Invoke(interaction, args);
            if (result is bool ok && ok)
            {
                var shape = args[1];
                var funcType = args[2];
                if (shape != null)
                {
                    var entry = new JObject
                    {
                        ["shape_index"] = SafeIntProperty(shape, "Index") + 1, // Convert zero-based to one-based
                        ["shape_name"] = SafeStringProperty(shape, "Name"),
                        ["function_type"] = funcType?.ToString() ?? "unknown",
                    };
                    return entry;
                }
            }
            return null;
        }
        catch
        {
            return null;
        }
    }

    private static object SafeInvokeWithArgs(object obj, string methodName, object[] args)
    {
        if (obj == null) return null;
        try
        {
            // Try to find a method with the right parameter count
            foreach (var method in obj.GetType().GetMethods(BindingFlags.Instance | BindingFlags.Public))
            {
                if (method.Name == methodName && method.GetParameters().Length == args.Length)
                {
                    return method.Invoke(obj, args);
                }
            }
            return null;
        }
        catch
        {
            return null;
        }
    }

    #endregion

        private JObject GetPredatorsOf(JObject parameters)
        {
            var core = RequireCore();
            var maxGroups = SafeIntProperty(core, "nGroups");

            var preyName = OptionalString(parameters, "name");
            int? preyIndexOpt = OptionalInt(parameters, "index");
            int preyIndex;

            if (!string.IsNullOrWhiteSpace(preyName))
            {
                // Resolve name to index (case-insensitive)
                preyIndex = 0;
                for (var i = 1; i <= maxGroups; i++)
                {
                    var g = SafeGetIndexedProperty(core, "EcopathGroupInputs", i);
                    if (g != null && string.Equals(SafeStringProperty(g, "Name"), preyName.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        preyIndex = i;
                        break;
                    }
                }
                if (preyIndex == 0)
                {
                    throw new EwEMcpException("name_not_found", $"Group name '{preyName}' not found");
                }
            }
            else if (preyIndexOpt.HasValue)
            {
                preyIndex = preyIndexOpt.Value;
                if (preyIndex < 1 || preyIndex > maxGroups)
                {
                    throw new EwEMcpException("index_out_of_range", $"Prey group index {preyIndex} is outside 1..{maxGroups}");
                }
            }
            else
            {
                throw new EwEMcpException("missing_parameter", "Either 'name' or 'index' is required");
            }

            var preyInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", preyIndex);
            preyName = SafeStringProperty(preyInput, "Name");

            var predators = new JArray();
            for (var predIdx = 1; predIdx <= maxGroups; predIdx++)
            {
                if (predIdx == preyIndex) continue;

                var predInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", predIdx);
                if (predInput == null) continue;

                var dietAmount = SafeGetIndexedProperty(predInput, "DietComp", preyIndex);
                if (!IsNonZero(dietAmount)) continue;

                var entry = new JObject
                {
                    ["index"] = predIdx,
                    ["name"] = SafeStringProperty(predInput, "Name"),
                    ["diet_fraction"] = ToJToken(dietAmount)
                };

                try
                {
                    // VulMult on a group's Ecosim input is indexed by PREDATOR group
                    // (confirmed by EwE source cEcoSimGroupInput.VulMult(iPredGroup)).
                    // So EcoSimGroupInputs[prey].VulMult[predator] = vulnerability of prey to predator.
                    var ecosimInput = SafeGetIndexedProperty(core, "EcosimGroupInputs", preyIndex);
                    if (ecosimInput != null)
                    {
                        var vulMult = SafeGetIndexedProperty(ecosimInput, "VulMult", predIdx);
                        if (vulMult != null)
                        {
                            entry["vul_mult"] = ToJToken(vulMult);
                        }
                    }
                }
                catch { }

                predators.Add(entry);
            }

            return new JObject
            {
                ["prey_index"] = preyIndex,
                ["prey_name"] = preyName,
                ["predators"] = predators,
                ["total_predators"] = predators.Count
            };
        }

        private JObject GetEcosimGroupResults(JObject parameters)
        {
            var core = RequireCore();
            EnsureEcosimRan(core);

            var listAll = OptionalBool(parameters, "list");

            if (listAll)
            {
                var groupCount = SafeIntProperty(core, "nGroups");
                var results = new JArray();
                for (var i = 1; i <= groupCount; i++)
                {
                    results.Add(BuildEcosimGroupResult(core, i, parameters));
                }
                return new JObject
                {
                    ["groups"] = results,
                    ["total_count"] = groupCount
                };
            }

            // Accept "index" or "scenario_index" as the group index
            var index = OptionalInt(parameters, "index")
                ?? OptionalInt(parameters, "scenario_index")
                ?? throw new EwEMcpException("missing_parameter", "Missing required integer parameter: index");
            var maxGroup = SafeIntProperty(core, "nGroups");
            if (index < 1 || index > maxGroup)
            {
                throw new EwEMcpException("index_out_of_range", $"Ecosim group result index {index} is outside 1..{maxGroup}");
            }
            return BuildEcosimGroupResult(core, index, parameters);
        }

        private JObject BuildEcosimGroupResult(cCore core, int index, JObject parameters)
        {
            var output = SafeGetIndexedProperty(core, "EcosimGroupOutputs", index);
            if (output == null)
            {
                throw new EwEMcpException("entity_unavailable", $"Ecosim group output {index} is unavailable. Run Ecosim first.");
            }

            var timeRange = EcosimTimeRange(core, parameters);
            var variables = OptionalStringArray(parameters, "variables") ?? new[]
            {
                "biomass",
                "biomass_rel",
                "catch",
                "feeding_time",
                "fish_mort",
                "total_mort"
            };
            int? fleetIndex = OptionalInt(parameters, "fleet_index");
            var series = new JObject();
            foreach (var variable in variables)
            {
                series[variable] = ReadEcosimGroupSeries(output, variable, timeRange.Item1, timeRange.Item2, fleetIndex);
            }

            return new JObject
            {
                ["index"] = index,
                ["name"] = SafeStringProperty(output, "Name"),
                ["time_start"] = timeRange.Item1,
                ["time_end"] = timeRange.Item2,
                ["time_steps"] = SafeIntProperty(core, "nEcosimTimeSteps"),
                ["first_year"] = SafeInvokeInt(core, "EcosimFirstYear"),
                ["summary"] = new JObject
                {
                    ["biomass_start"] = SafeJTokenProperty(output, "BiomassStart"),
                    ["biomass_end"] = SafeJTokenProperty(output, "BiomassEnd"),
                    ["is_catch_aggregated"] = SafeJTokenProperty(output, "isCatchAggregated")
                },
                ["series"] = series
            };
        }

        private JObject GetEcosimFleetResults(JObject parameters)
        {
            var core = RequireCore();
            EnsureEcosimRan(core);
            var index = RequiredInt(parameters, "index");
            var fleetCount = SafeIntProperty(core, "nFleets");
            if (index < 1 || index > fleetCount)
            {
                throw new EwEMcpException("index_out_of_range", $"Ecosim fleet result index {index} is outside 1..{fleetCount}");
            }
            var output = SafeGetIndexedProperty(core, "EcosimFleetOutput", index);
            if (output == null)
            {
                throw new EwEMcpException("entity_unavailable", $"Ecosim fleet output {index} is unavailable. Run Ecosim first.");
            }

            var timeRange = EcosimTimeRange(core, parameters);
            var variables = OptionalStringArray(parameters, "variables") ?? new[] { "catch_biomass", "value" };
            var series = new JObject();
            foreach (var variable in variables)
            {
                series[variable] = ReadEcosimFleetSeries(output, variable, timeRange.Item1, timeRange.Item2);
            }

            return new JObject
            {
                ["index"] = index,
                ["time_start"] = timeRange.Item1,
                ["time_end"] = timeRange.Item2,
                ["time_steps"] = SafeIntProperty(core, "nEcosimTimeSteps"),
                ["first_year"] = SafeInvokeInt(core, "EcosimFirstYear"),
                ["summary"] = ReadScalarValues(output, null, false, "ecosim_fleet_outputs"),
                ["series"] = series
            };
        }

        private JObject ExportEcosimResults(JObject parameters)
        {
            var path = RequiredString(parameters, "path");
            // Validate path
            if (string.IsNullOrWhiteSpace(path))
            {
                throw new EwEMcpException("missing_parameter", "path is required");
            }
            var ext = System.IO.Path.GetExtension(path).ToLowerInvariant();
            if (ext != ".csv" && ext != ".txt")
            {
                throw new EwEMcpException("invalid_parameter", "path must have a .csv or .txt extension");
            }
            if (path.Contains(".."))
            {
                throw new EwEMcpException("invalid_parameter", "path must not contain directory traversal");
            }
            var core = RequireCore();
            var method = core.GetType().GetMethods(BindingFlags.Instance | BindingFlags.Public)
                .FirstOrDefault(m =>
                    string.Equals(m.Name, "dumpEcosimModelResults", StringComparison.OrdinalIgnoreCase) &&
                    m.GetParameters().Length == 1 &&
                    m.GetParameters()[0].ParameterType == typeof(string));
            if (method == null)
            {
                throw new EwEMcpException("export_unavailable", "This EwE build does not expose dumpEcosimModelResults.");
            }

            var ok = Convert.ToBoolean(method.Invoke(core, new object[] { path }));
            return new JObject { ["exported"] = ok, ["path"] = path };
        }

        private void EnsureEcosimRan(cCore core)
        {
            var sm = SafeGetProperty(core, "StateMonitor");
            if (!SafeInvokeBool(sm, "HasEcosimRan"))
            {
                throw new EwEMcpException("ecosim_results_unavailable", "Run Ecosim before reading Ecosim result time-series.");
            }
        }

        private Tuple<int, int> EcosimTimeRange(cCore core, JObject parameters)
        {
            var max = SafeIntProperty(core, "nEcosimTimeSteps");
            if (max < 1)
            {
                throw new EwEMcpException("ecosim_results_unavailable", "Ecosim has no result timesteps.");
            }
            var start = OptionalInt(parameters, "start_time") ?? 1;
            var end = OptionalInt(parameters, "end_time") ?? Math.Min(max, start + 119);
            if (start < 1 || start > max || end < start || end > max)
            {
                throw new EwEMcpException("time_range_out_of_range", $"Ecosim time range {start}..{end} is outside 1..{max}");
            }
            return Tuple.Create(start, end);
        }

        private JArray ReadEcosimGroupSeries(object output, string variable, int start, int end, int? fleetIndex)
        {
            string propertyName;
            switch ((variable ?? string.Empty).Trim().ToLowerInvariant())
            {
                case "biomass": propertyName = "Biomass"; break;
                case "biomass_rel": propertyName = "BiomassRel"; break;
                case "catch":
                case "yield": propertyName = "Catch"; break;
                case "catch_rel": propertyName = "CatchRel"; break;
                case "feeding_time": propertyName = "FeedingTime"; break;
                case "fish_mort": propertyName = "FishMort"; break;
                case "total_mort": propertyName = "TotalMort"; break;
                case "pred_mort": propertyName = "PredMort"; break;
                case "consump_biomass": propertyName = "ConsumpBiomass"; break;
                case "tl": propertyName = "TL"; break;
                case "value": propertyName = "Value"; break;
                case "value_rel": propertyName = "ValueRel"; break;
                case "avg_weight": propertyName = "AvgWeight"; break;
                case "prod_consump": propertyName = "ProdConsump"; break;
                case "catch_by_fleet": propertyName = "CatchByFleet"; break;
                case "fishing_mort_by_fleet": propertyName = "FishingMortByFleet"; break;
                case "value_by_fleet": propertyName = "ValueByFleet"; break;
                case "landings_by_fleet": propertyName = "LandingsByFleet"; break;
                case "discard_by_fleet": propertyName = "DiscardByFleet"; break;
                default:
                    throw new EwEMcpException("unknown_ecosim_result_variable", $"Unknown Ecosim group result variable: {variable}");
            }

            var needsFleet = propertyName.EndsWith("ByFleet", StringComparison.OrdinalIgnoreCase);
            if (needsFleet && !fleetIndex.HasValue)
            {
                throw new EwEMcpException("missing_parameter", $"{variable} requires fleet_index.");
            }

            var result = new JArray();
            for (var t = start; t <= end; t++)
            {
                var value = needsFleet
                    ? SafeGetIndexedProperty(output, propertyName, fleetIndex.Value, t)
                    : SafeGetIndexedProperty(output, propertyName, t);
                result.Add(new JObject { ["time"] = t, ["value"] = ToJToken(value) });
            }
            return result;
        }

        private JArray ReadEcosimFleetSeries(object output, string variable, int start, int end)
        {
            string propertyName;
            switch ((variable ?? string.Empty).Trim().ToLowerInvariant())
            {
                case "catch":
                case "catch_biomass":
                    propertyName = "CatchBiomass";
                    break;
                case "value":
                    propertyName = "Value";
                    break;
                default:
                    throw new EwEMcpException("unknown_ecosim_result_variable", $"Unknown Ecosim fleet result variable: {variable}");
            }

            var result = new JArray();
            for (var t = start; t <= end; t++)
            {
                result.Add(new JObject { ["time"] = t, ["value"] = ToJToken(SafeGetIndexedProperty(output, propertyName, t)) });
            }
            return result;
        }

        private JObject ListUiCommands()
        {
            var cmdh = _commandHandler();
            var commands = new JArray();
            if (cmdh == null)
            {
                return new JObject { ["commands"] = commands, ["total_count"] = 0 };
            }

            var field = typeof(cCommandHandler).GetField("m_dictCommands", BindingFlags.Instance | BindingFlags.NonPublic);
            if (!(field?.GetValue(cmdh) is System.Collections.IDictionary dict))
            {
                return new JObject { ["commands"] = commands, ["total_count"] = 0 };
            }

            foreach (System.Collections.DictionaryEntry entry in dict)
            {
                if (entry.Value is cCommand cmd)
                {
                    commands.Add(new JObject
                    {
                        ["name"] = cmd.Name,
                        ["description"] = cmd.Description,
                        ["enabled"] = cmd.Enabled,
                        ["checked"] = cmd.Checked,
                        ["is_available"] = cmd.IsAvailable,
                        ["status"] = cmd.Status,
                        ["is_invoking"] = SafeBoolProperty(cmd, "IsInvoking")
                    });
                }
            }
            return new JObject { ["commands"] = commands, ["total_count"] = commands.Count };
        }

        // Dialog-opening commands that are blocked in MCP mode.
        // Extend via EWE_MCP_DIALOG_BLOCKED_COMMANDS env var (comma-separated) or
        // dialog_blocked_commands key in EwE_MCP_Plugin.cfg (comma-separated).
        private static readonly HashSet<string> DefaultDialogOpeningCommands = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "ImportTimeSeries",
            "m_cmdImportTimeSeries",
        };

        private HashSet<string> GetDialogOpeningCommands()
        {
            if (_dialogOpeningCommands != null)
            {
                return _dialogOpeningCommands;
            }

            var commands = new HashSet<string>(DefaultDialogOpeningCommands, StringComparer.OrdinalIgnoreCase);
            var extra = _config.DialogBlockedCommands;
            if (extra != null)
            {
                foreach (var cmd in extra.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
                {
                    commands.Add(cmd.Trim());
                }
            }
            _dialogOpeningCommands = commands;
            return _dialogOpeningCommands;
        }

        private JObject InvokeUiCommand(JObject parameters)
        {
            var name = RequiredString(parameters, "name");
            var cmdh = RequireCommandHandler();
            var cmd = cmdh.GetCommand(name);
            if (cmd == null)
            {
                throw new EwEMcpException("command_not_found", $"UI command not found: {name}");
            }
            if (GetDialogOpeningCommands().Contains(name))
            {
                throw new EwEMcpException("dialog_command_blocked", $"UI command '{name}' opens a modal dialog and is blocked in MCP mode to prevent socket hangs. Use a dedicated MCP tool when available, operate the EwE GUI manually, or enable dialog_watcher_enabled only for supervised workflows.");
            }

            if (parameters.TryGetValue("tag", out var tag))
            {
                cmd.Tag = tag.Type == JTokenType.Null ? null : ((JValue)tag).Value;
            }
            cmd.Invoke();
            return new JObject { ["invoked"] = true, ["name"] = cmd.Name, ["enabled"] = cmd.Enabled };
        }

        private JObject ListNavigationNodes()
        {
            var tree = RequireNavigationTreeView();
            var nodes = new JArray();
            var ordinal = 0;
            foreach (TreeNode node in tree.Nodes)
            {
                AddNavigationNode(nodes, node, null, 0, ref ordinal, tree.SelectedNode);
            }

            return new JObject
            {
                ["nodes"] = nodes,
                ["total_count"] = nodes.Count,
                ["selected"] = tree.SelectedNode == null ? null : BuildNavigationNode(tree.SelectedNode, null, -1, -1, tree.SelectedNode)
            };
        }

        private JObject NavigateToNode(JObject parameters)
        {
            var tree = RequireNavigationTreeView();
            var node = FindNavigationNode(tree, parameters ?? new JObject());
            if (node == null)
            {
                throw new EwEMcpException("navigation_node_not_found", "No navigation node matched the supplied id, name, text, path, or query.");
            }

            tree.SelectedNode = node;
            node.EnsureVisible();
            tree.Focus();
            RefreshUI();

            return new JObject
            {
                ["navigated"] = true,
                ["selected"] = BuildNavigationNode(node, null, -1, -1, node)
            };
        }

        private JObject GetActiveView()
        {
            var tree = FindNavigationTreeView();
            TreeNode selected = tree?.SelectedNode;
            var formMain = SafeGetProperty(_uiContext(), "FormMain");
            var activeForm = Form.ActiveForm ?? formMain as Form;
            return new JObject
            {
                ["has_navigation_tree"] = tree != null,
                ["selected"] = selected == null ? null : BuildNavigationNode(selected, null, -1, -1, selected),
                ["form_main_type"] = formMain?.GetType().FullName ?? string.Empty,
                ["active_form"] = activeForm?.GetType().FullName ?? string.Empty,
                ["active_form_source"] = Form.ActiveForm == null && activeForm != null ? "form_main_fallback" : "active_form"
            };
        }

        private TreeView RequireNavigationTreeView()
        {
            return FindNavigationTreeView()
                ?? throw new EwEMcpException("navigation_tree_unavailable", "EwE navigation tree is not available yet.");
        }

        private TreeView FindNavigationTreeView()
        {
            foreach (Form form in Application.OpenForms)
            {
                var tree = FindTreeViewByName(form, NavigationTreeName);
                if (tree != null)
                {
                    return tree;
                }
            }

            var formMain = SafeGetProperty(_uiContext(), "FormMain") as Control;
            return FindTreeViewByName(formMain, NavigationTreeName);
        }

        private static TreeView FindTreeViewByName(Control root, string name)
        {
            if (root == null)
            {
                return null;
            }

            if (root is TreeView tree && string.Equals(tree.Name, name, StringComparison.OrdinalIgnoreCase))
            {
                return tree;
            }

            foreach (Control child in root.Controls)
            {
                var found = FindTreeViewByName(child, name);
                if (found != null)
                {
                    return found;
                }
            }

            return null;
        }

        private static void AddNavigationNode(JArray nodes, TreeNode node, string parentId, int depth, ref int ordinal, TreeNode selected)
        {
            nodes.Add(BuildNavigationNode(node, parentId, depth, ordinal, selected));
            ordinal++;
            foreach (TreeNode child in node.Nodes)
            {
                AddNavigationNode(nodes, child, node.Name, depth + 1, ref ordinal, selected);
            }
        }

        private static JObject BuildNavigationNode(TreeNode node, string parentId, int depth, int ordinal, TreeNode selected)
        {
            return new JObject
            {
                ["id"] = node.Name ?? string.Empty,
                ["name"] = node.Name ?? string.Empty,
                ["text"] = node.Text ?? string.Empty,
                ["path"] = NavigationPath(node, useIds: false),
                ["id_path"] = NavigationPath(node, useIds: true),
                ["depth"] = depth >= 0 ? depth : NodeDepth(node),
                ["parent_id"] = parentId ?? node.Parent?.Name ?? string.Empty,
                ["ordinal"] = ordinal,
                ["tag_type"] = node.Tag?.GetType().FullName ?? string.Empty,
                ["has_children"] = node.Nodes.Count > 0,
                ["is_selected"] = ReferenceEquals(node, selected)
            };
        }

        private static string NavigationPath(TreeNode node, bool useIds)
        {
            var parts = new Stack<string>();
            var current = node;
            while (current != null)
            {
                var part = useIds ? current.Name : current.Text;
                parts.Push(part ?? string.Empty);
                current = current.Parent;
            }
            return string.Join(" > ", parts.ToArray());
        }

        private static int NodeDepth(TreeNode node)
        {
            var depth = 0;
            var current = node.Parent;
            while (current != null)
            {
                depth++;
                current = current.Parent;
            }
            return depth;
        }

        private static TreeNode FindNavigationNode(TreeView tree, JObject parameters)
        {
            var id = OptionalString(parameters, "id") ?? OptionalString(parameters, "name");
            var text = OptionalString(parameters, "text");
            var path = OptionalString(parameters, "path");
            var query = OptionalString(parameters, "query");
            if (string.IsNullOrWhiteSpace(id) && string.IsNullOrWhiteSpace(text) &&
                string.IsNullOrWhiteSpace(path) && string.IsNullOrWhiteSpace(query))
            {
                throw new EwEMcpException("missing_parameter", "Provide one of id, name, text, path, or query.");
            }

            if (!string.IsNullOrWhiteSpace(id))
            {
                var matches = tree.Nodes.Find(id, true);
                if (matches.Length > 0)
                {
                    return matches[0];
                }
            }

            var all = FlattenNavigationNodes(tree);
            return all.FirstOrDefault(n => Matches(n.Name, id)) ??
                   all.FirstOrDefault(n => Matches(n.Text, text)) ??
                   all.FirstOrDefault(n => Matches(NavigationPath(n, useIds: false), path)) ??
                   all.FirstOrDefault(n => Matches(NavigationPath(n, useIds: true), path)) ??
                   all.FirstOrDefault(n => Contains(n.Name, query) || Contains(n.Text, query) ||
                                           Contains(NavigationPath(n, useIds: false), query) ||
                                           Contains(NavigationPath(n, useIds: true), query));
        }

        private static List<TreeNode> FlattenNavigationNodes(TreeView tree)
        {
            var nodes = new List<TreeNode>();
            foreach (TreeNode node in tree.Nodes)
            {
                FlattenNavigationNodes(node, nodes);
            }
            return nodes;
        }

        private static void FlattenNavigationNodes(TreeNode node, List<TreeNode> nodes)
        {
            nodes.Add(node);
            foreach (TreeNode child in node.Nodes)
            {
                FlattenNavigationNodes(child, nodes);
            }
        }

        private static bool Matches(string value, string expected)
        {
            return !string.IsNullOrWhiteSpace(expected) &&
                   string.Equals(value ?? string.Empty, expected, StringComparison.OrdinalIgnoreCase);
        }

        private static bool Contains(string value, string query)
        {
            return !string.IsNullOrWhiteSpace(query) &&
                   (value ?? string.Empty).IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private JObject NavigateToSuggestedEditNode(string domain, IEnumerable<string> variables)
        {
            var nodeId = SuggestedEditNodeId(domain, variables);
            if (string.IsNullOrWhiteSpace(nodeId))
            {
                return new JObject
                {
                    ["attempted"] = false,
                    ["reason"] = "no_suggestion",
                    ["domain"] = domain
                };
            }

            try
            {
                var result = NavigateToNode(new JObject { ["id"] = nodeId });
                result["attempted"] = true;
                result["suggested_node_id"] = nodeId;
                return result;
            }
            catch (EwEMcpException ex)
            {
                return new JObject
                {
                    ["attempted"] = true,
                    ["suggested_node_id"] = nodeId,
                    ["error"] = ex.Code,
                    ["message"] = ex.Message
                };
            }
        }

        private static string SuggestedEditNodeId(string domain, IEnumerable<string> variables)
        {
            string normalized;
            try
            {
                normalized = NormalizeDomain(domain);
            }
            catch (EwEMcpException)
            {
                return string.Empty;
            }
            var vars = (variables ?? Enumerable.Empty<string>())
                .Select(v => (v ?? string.Empty).Trim().ToLowerInvariant())
                .ToArray();

            if (normalized == "ecopath_groups")
            {
                if (vars.Any(v => v == "dietcomp" || v == "diet_comp" || v == "dietcomposition" || v == "diet_composition"))
                {
                    return NodeDietComposition;
                }
                return NodeBasicInput;
            }

            if (normalized == "ecopath_fleets")
            {
                if (vars.Any(v => v == "landings" || v == "landing"))
                {
                    return NodeLandings;
                }
                if (vars.Any(v => v == "discards" || v == "discard"))
                {
                    return NodeDiscards;
                }
                return NodeFleetDefinition;
            }

            if (normalized == "ecosim_group_inputs")
            {
                return NodeEcosimGroupInfo;
            }

            if (normalized == "ecosim_fleet_inputs")
            {
                return NodeEcosimFleetSizeDynamics;
            }

            return string.Empty;
        }

        private JObject CreateModel(JObject parameters)
        {
            var core = RequireCore();
            var path = RequiredString(parameters, "path");
            var modelName = OptionalString(parameters, "model_name") ??
                System.IO.Path.GetFileNameWithoutExtension(path);
            var author = OptionalString(parameters, "author") ?? core.GetType()
                .GetProperty("DefaultAuthor")?.GetValue(core)?.ToString() ?? "";

            // Create a new Access database via cEwEAccessDatabase.Create
            // Try several possible type names for cEwEAccessDatabase
            Type dbType = null;
            var assembly = core.GetType().Assembly;
            foreach (var tn in new[] {
                "EwECore.cEwEAccessDatabase",
                "EwECore.Database.cEwEAccessDatabase",
                "EwECore.DataSource.cEwEAccessDatabase",
                "EwECore.DataSources.cEwEAccessDatabase",
            })
            {
                dbType = assembly.GetType(tn);
                if (dbType != null) break;
            }
            if (dbType == null)
                throw new EwEMcpException("unsupported_operation", "cEwEAccessDatabase not available in this EwE build");

            object db = Activator.CreateInstance(dbType);
            // Get the Create method by matching exact parameter types from
            // the first candidate we find.  The 4th param is eDataSourceTypes.
            var createMethod = dbType.GetMethods()
                .FirstOrDefault(m => m.Name == "Create" && m.GetParameters().Length == 5);
            if (createMethod == null)
                throw new EwEMcpException("unsupported_operation", "cEwEAccessDatabase.Create method not available");
            // Use the actual parameter type from the method for the enum
            var actualDsType = createMethod.GetParameters()[3].ParameterType;
            object dsType = Enum.ToObject(actualDsType, 3); // eDataSourceTypes.Access2007 = 3

            var result = createMethod.Invoke(db, new object[] { path, modelName, true, dsType, author });
            // eDatasourceAccessType.Success = 0
            var successVal = Convert.ToInt32(result);
            if (successVal != 0)
                throw new EwEMcpException("create_failed", $"Failed to create model (result={successVal})");

            // Load the newly created model
            bool loaded = core.LoadModel(path);
            return new JObject
            {
                ["created"] = true,
                ["loaded"] = loaded,
                ["path"] = path,
                ["model_name"] = modelName,
            };
        }

        private JObject LoadModel(JObject parameters)
        {
            var path = RequiredString(parameters, "path");
            var core = RequireCore();
            var ok = core.LoadModel(path);

            // After loading, re-apply any pending diet writes to the in-memory
            // entity DietComp properties.  SaveEcopathDietComp writes to the
            // EcopathDietComp table during core.Save(), but core.LoadModel may
            // not populate the DietComp property for groups that were created
            // after the model was first opened.  By re-applying pending writes
            // after every load we ensure the in-memory state stays correct.
            ApplyPendingDietWritesInMemory(core);

            // Also flush any SQL-persisted writes that may have been orphaned
            // by the load (re-populates the EcopathDietComp table for entries
            // that EwE's own load may have missed).
            FlushPendingDietWrites();

            return new JObject { ["loaded"] = ok, ["path"] = path };
        }

        private JObject SaveModel(JObject parameters, bool saveAs)
        {
            var core = RequireCore();
            string path = parameters?["path"]?.ToString();
            if (!string.IsNullOrWhiteSpace(path))
            {
                // Check raw input for directory traversal before path resolution
                if (path.Contains(".."))
                {
                    throw new EwEMcpException("invalid_parameter", "path must not contain directory traversal");
                }
            }
            bool ok;
            if (string.IsNullOrWhiteSpace(path))
                ok = core.Save();
            else
                ok = core.Save(path);

            // After core.Save() triggers SaveEcopathDietComp (DELETE+INSERT from the
            // in-memory DC array), re-apply any pending diet writes for newly created
            // groups whose DC entries are not covered by the array.
            if (ok)
            {
                FlushPendingDietWrites();
            }

            return new JObject { ["saved"] = ok, ["path"] = path ?? string.Empty };
        }

        /// <summary>
        /// Strip password/credentials from an OLEDB connection string to prevent
        /// credential leakage via exception messages or logging.
        /// Also ensures Persist Security Info is set to False.
        /// </summary>
        private static string SanitizeConnectionString(string cs)
        {
            if (string.IsNullOrEmpty(cs)) return cs;
            try
            {
                var builder = new System.Data.OleDb.OleDbConnectionStringBuilder(cs);
                // Remove any password
                if (builder.ContainsKey("Jet OLEDB:Database Password"))
                    builder.Remove("Jet OLEDB:Database Password");
                if (builder.ContainsKey("Password"))
                    builder.Remove("Password");
                // Prevent credentials from being discoverable via ConnectionString property
                builder["Persist Security Info"] = "False";
                return builder.ConnectionString;
            }
            catch
            {
                // If we can't parse it, strip common credential tokens manually
                var sanitized = System.Text.RegularExpressions.Regex.Replace(cs,
                    @"(Jet OLEDB:Database Password|Password|Pwd)=[^;]*;?",
                    "", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                sanitized = System.Text.RegularExpressions.Regex.Replace(sanitized,
                    @"Persist Security Info=[^;]*;?",
                    "Persist Security Info=False;", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                return sanitized.TrimEnd(';');
            }
        }

        /// <summary>
        /// Get an OLEDB connection string for the current model by trying multiple
        /// property access patterns against the EwE core and its DataSource.
        /// Returns the raw connection string (including credentials if stored)
        /// so that database connections work for password-protected .EwEmdb files.
        /// For logging or error surfacing, use SanitizeConnectionString() separately.
        /// </summary>
        private static string GetConnectionString(object core)
        {
            if (core == null) return null;

            var ds = SafeGetProperty(core, "DataSource");
            if (ds == null) return null;

            // Path 1: DataSource.Connection → cEwEAccessDatabase.m_conn (OleDbConnection) → ConnectionString
            try
            {
                var connObj = SafeGetProperty(ds, "Connection");
                if (connObj != null)
                {
                    var oleDbConn = SafeGetProperty(connObj, "m_conn");
                    if (oleDbConn != null)
                    {
                        var cs = SafeGetProperty(oleDbConn, "ConnectionString") as string;
                        if (!string.IsNullOrEmpty(cs))
                        {
                            return cs;
                        }
                    }
                    var cs2 = SafeGetProperty(connObj, "ConnectionString") as string;
                    if (!string.IsNullOrEmpty(cs2))
                    {
                        return cs2;
                    }
                }
            }
            catch { }

            // Path 2: DataSource.Directory() + DataSource.FileName() + DataSource.Extension()
            try
            {
                var dir = SafeInvoke(ds, "Directory") as string;
                var fn = SafeInvoke(ds, "FileName") as string;
                var ext = SafeInvoke(ds, "Extension") as string;
                if (!string.IsNullOrEmpty(dir) && !string.IsNullOrEmpty(fn))
                {
                    var fullPath = System.IO.Path.Combine(dir, fn);
                    if (!string.IsNullOrEmpty(ext)) fullPath += ext;
                    else if (!fullPath.EndsWith(".EwEmdb", StringComparison.OrdinalIgnoreCase))
                        fullPath += ".EwEmdb";
                    return $"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={fullPath};Persist Security Info=False;";
                }
            }
            catch { }

            // Path 3: DataSource.ToString() as fallback
            try
            {
                var toString = SafeInvoke(ds, "ToString") as string;
                if (!string.IsNullOrEmpty(toString) && toString.EndsWith(".EwEmdb", StringComparison.OrdinalIgnoreCase))
                {
                    return $"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={toString};Persist Security Info=False;";
                }
            }
            catch { }

            return null;
        }

        /// <summary>
        /// Re-apply pending diet_comp writes to the in-memory entity
        /// DietComp indexed properties.  This is called after core.LoadModel()
        /// to restore diet_comp values that may not have been loaded from the
        /// EcopathDietComp table (e.g. for groups created via AddGroup whose
        /// GroupIDs differ from those used in the SQL table).
        /// 
        /// Uses SafeGetIndexedProperty instead of nGroups for bounds checking
        /// because nGroups may not reflect newly added groups.
        /// </summary>
        private void ApplyPendingDietWritesInMemory(object core)
        {
            if (core == null) return;

            KeyValuePair<(int, int), float>[] pending;
            lock (_dietLock)
            {
                if (_pendingDietWrites.Count == 0) return;
                pending = _pendingDietWrites.ToArray();
            }
            // Don't clear _pendingDietWrites — FlushPendingDietWrites preserves
            // the list so writes survive repeated load_model cycles. The
            // Dictionary automatically deduplicates by (PredIndex, PreyIndex).

            foreach (var kvp in pending)
            {
                int predIndex = kvp.Key.Item1;
                int preyIndex = kvp.Key.Item2;
                float dietVal = kvp.Value;
                try
                {
                    // Don't use nGroups for bounds — it may not reflect AddGroup.
                    var input = SafeGetIndexedProperty(core, "EcopathGroupInputs", predIndex);
                    if (input == null)
                    {
                        EwEMcpFileLog.Warn($"ApplyPendingDietWritesInMemory: no entity at index {predIndex}");
                        continue;
                    }
                    var prop = FindIndexedProperty(input, "diet_comp", "ecopath_groups", 1, requireWritable: true);
                    if (prop != null)
                    {
                        prop.SetValue(input, dietVal, new object[] { preyIndex });

                        // Also update the DietComp[] array property so get_values sees
                        // the value. After load_model the entity re-initializes its
                        // DietComp array as 1-based (index 1 = group 1, index 0 unused).
                        var arrayProp = FindWritableProperty(input, "diet_comp", "ecopath_groups");
                        if (arrayProp != null && preyIndex >= 1)
                        {
                            var dietArray = arrayProp.GetValue(input, null) as float[];
                            if (dietArray != null && preyIndex < dietArray.Length)
                            {
                                dietArray[preyIndex] = dietVal;
                            }
                        }

                        EwEMcpFileLog.Info($"ApplyPendingDietWritesInMemory: set [{predIndex},{preyIndex}] = {dietVal}");
                    }
                }
                catch (Exception ex)
                {
                    EwEMcpFileLog.Warn($"ApplyPendingDietWritesInMemory: failed for [{predIndex},{preyIndex}]: {ex.Message}");
                }
            }
        }

        /// <summary>
        /// Re-apply pending diet_comp writes directly to the database after
        /// core.Save() has finished.  SaveEcopathDietComp does DELETE+INSERT
        /// from the in-memory DC array, which contains zeros for newly created
        /// groups (since the DC array was never updated for those entries).
        /// This method resolves 1-based group indices from the pending writes
        /// to current GroupIDs and writes them via SQL so that a subsequent
        /// load_model reads the correct values.
        /// </summary>
        private void FlushPendingDietWrites()
        {
            // Copy pending entries but DON'T clear the dictionary — writes must survive
            // across load_model cycles so that ApplyPendingDietWritesInMemory can
            // re-apply them after each reload.  The Dictionary automatically deduplicates
            // by (PredIndex, PreyIndex), preventing unbounded growth on repeated edits.
            KeyValuePair<(int, int), float>[] pending;
            lock (_dietLock)
            {
                if (_pendingDietWrites.Count == 0) return;
                pending = _pendingDietWrites.ToArray();
            }

            var core = _core();
            if (core == null) return;

            var connStr = GetConnectionString(core);
            if (string.IsNullOrEmpty(connStr))
            {
                EwEMcpFileLog.Warn("FlushPendingDietWrites: no connection string available");
                return;
            }

            try
            {
                using (var conn = new OleDbConnection(connStr))
                {
                    conn.Open();
                    foreach (var kvp in pending)
                    {
                        int predIndex = kvp.Key.Item1;
                        int preyIndex = kvp.Key.Item2;
                        float dietVal = kvp.Value;
                        try
                        {
                            // Don't use nGroups for bounds — it may not reflect AddGroup.
                            // Instead, try to resolve the entity and skip if unavailable.
                            var predInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", predIndex);
                            var preyInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", preyIndex);
                            if (predInput == null || preyInput == null)
                            {
                                EwEMcpFileLog.Warn($"FlushPendingDietWrites: entity unavailable at predIdx={predIndex} preyIdx={preyIndex}");
                                continue;
                            }

                            int predId = Convert.ToInt32(
                                SafeGetProperty(predInput, "GroupID") ?? SafeGetProperty(predInput, "DBID"));
                            int preyId = Convert.ToInt32(
                                SafeGetProperty(preyInput, "GroupID") ?? SafeGetProperty(preyInput, "DBID"));

                            using (var cmd = new OleDbCommand(
                                "UPDATE [EcopathDietComp] SET [Diet] = @v WHERE [PredID] = @p1 AND [PreyID] = @p2",
                                conn))
                            {
                                cmd.Parameters.AddWithValue("@v", dietVal);
                                cmd.Parameters.AddWithValue("@p1", predId);
                                cmd.Parameters.AddWithValue("@p2", preyId);
                                int rows = cmd.ExecuteNonQuery();
                                if (rows == 0)
                                {
                                    using (var insertCmd = new OleDbCommand(
                                        "INSERT INTO [EcopathDietComp] ([PredID], [PreyID], [Diet]) VALUES (@p1, @p2, @v)",
                                        conn))
                                    {
                                        insertCmd.Parameters.AddWithValue("@p1", predId);
                                        insertCmd.Parameters.AddWithValue("@p2", preyId);
                                        insertCmd.Parameters.AddWithValue("@v", dietVal);
                                        insertCmd.ExecuteNonQuery();
                                    }
                                }
                            }
                        }
                        catch (Exception ex)
                        {
                            EwEMcpFileLog.Warn($"FlushPendingDietWrites: failed for predIdx={predIndex} preyIdx={preyIndex}: {ex.GetType().Name}");
                        }
                    }
                }
                EwEMcpFileLog.Info($"FlushPendingDietWrites: flushed {pending.Length} diet entries");
            }
            catch (Exception ex)
            {
                EwEMcpFileLog.Warn("FlushPendingDietWrites: connection failed — see plugin log for details");
                EwEMcpFileLog.Info($"FlushPendingDietWrites: connection exception type: {ex.GetType().Name}");
            }
        }

        private JObject CloseModel()
        {
            bool ok = RequireCore().CloseModel();
            lock (_dietLock)
            {
                _pendingDietWrites.Clear();
            }
            return new JObject { ["closed"] = ok };
        }

        private JObject HasChanges()
        {
            var core = RequireCore();
            return new JObject { ["has_changes"] = core.HasChanges() };
        }

        private JObject DiscardChanges()
        {
            var core = RequireCore();
            var hadChanges = core.HasChanges();
            var ok = core.DiscardChanges();
            return new JObject
            {
                ["discarded"] = ok,
                ["had_changes"] = hadChanges,
                ["has_changes"] = core.HasChanges()
            };
        }

        private JObject RunEcopath()
        {
            var core = RequireCore();
            bool ok = core.RunEcopath();
            bool isBalanced = false;
            try { isBalanced = (bool)SafeInvokeBool(core, "IsModelBalanced"); } catch { }
            string runDetail = SafeGetEcopathRunStateDetail(core);
            string runState = isBalanced ? "balanced" : "unbalanced";
            return new JObject
            {
                ["started"] = ok,
                ["run_type"] = "ecopath",
                ["is_model_balanced"] = isBalanced,
                ["ecopath_run_state"] = runState,
                ["ecopath_detail"] = runDetail
            };
        }

        private JObject GetEcopathRunState()
        {
            var core = RequireCore();
            var sm = SafeGetProperty(core, "StateMonitor");
            bool hasEcopathRan = false;
            bool isBalanced = false;
            try { hasEcopathRan = SafeInvokeBool(sm, "HasEcopathRan"); } catch { }
            try { isBalanced = SafeInvokeBool(core, "IsModelBalanced"); } catch { }
            string runDetail = SafeGetEcopathRunStateDetail(core);
            string runState;
            if (hasEcopathRan && isBalanced)
            {
                runState = "balanced";
            }
            else if (hasEcopathRan)
            {
                runState = "unbalanced";
            }
            else
            {
                runState = "not_run";
            }
            return new JObject
            {
                ["core_execution_state"] = SafeInvokeToString(sm, "CoreExecutionState"),
                ["has_ecopath_loaded"] = SafeInvokeBool(sm, "HasEcopathLoaded"),
                ["has_ecopath_ran"] = hasEcopathRan,
                ["is_model_balanced"] = isBalanced,
                ["ecopath_run_state"] = runState,
                ["ecopath_detail"] = runDetail,
                ["queried_at"] = DateTime.UtcNow.ToString("o")
            };
        }

        private static string SafeGetEcopathRunStateDetail(object core)
        {
            try
            {
                var ecopath = SafeGetMember(core, "Ecopath") ?? SafeGetMember(core, "m_Ecopath");
                if (ecopath != null)
                {
                    var field = ecopath.GetType().GetField(
                        "RunState",
                        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    if (field != null)
                    {
                        var val = field.GetValue(ecopath);
                        return val?.ToString() ?? "unknown";
                    }
                }
            }
            catch { }
            return "unknown";
        }

        private static object SafeGetMember(object obj, string name)
        {
            if (obj == null)
            {
                return null;
            }

            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
            var type = obj.GetType();
            var property = type.GetProperty(name, flags);
            if (property != null && property.GetIndexParameters().Length == 0)
            {
                return property.GetValue(obj, null);
            }
            var field = type.GetField(name, flags);
            return field?.GetValue(obj);
        }

        private JObject GetEcopathStats()
        {
            var core = RequireCore();
            var stats = SafeGetProperty(core, "EcopathStats");
            if (stats == null)
            {
                throw new EwEMcpException("stats_unavailable", "Ecopath statistics not available; run Ecopath first");
            }
            var values = ReadScalarValues(stats, null, true, "ecopath_stats");
            return new JObject
            {
                ["values"] = values,
                ["available"] = true
            };
        }

        private JObject GetEcopathGroupDiagnostics()
        {
            var core = RequireCore();
            var count = SafeIntProperty(core, "nGroups");
            var groups = new JArray();
            var diagVars = new[] { "EEOutput", "EEOutputStatus", "PBOutput", "PBOutputStatus", "GEOutput", "GEOutputStatus", "TTLX", "Biomass" };

            for (var i = 1; i <= count; i++)
            {
                var output = SafeGetIndexedProperty(core, "EcopathGroupOutputs", i);
                var input = SafeGetIndexedProperty(core, "EcopathGroupInputs", i);
                if (output == null || input == null) continue;
                var group = new JObject
                {
                    ["index"] = i,
                    ["name"] = SafeStringProperty(input, "Name"),
                    ["is_living"] = !SafeBoolProperty(input, "IsDetritus"),
                    ["values"] = ReadScalarValues(output, diagVars, true, "ecopath_group_outputs")
                };
                groups.Add(group);
            }
            return new JObject { ["groups"] = groups, ["total_count"] = count };
        }

        private JObject GetEcopathFleetDiagnostics()
        {
            var core = RequireCore();
            var fleetCount = SafeIntProperty(core, "nFleets");
            var groupCount = SafeIntProperty(core, "nGroups");
            var fleets = new JArray();

            for (var f = 1; f <= fleetCount; f++)
            {
                var fleetInput = SafeGetIndexedProperty(core, "EcopathFleetInputs", f);
                var fleetOutput = SafeGetIndexedProperty(core, "EcopathFleetOutputs", f);
                if (fleetInput == null || fleetOutput == null) continue;

                var catchByGroup = new JArray();
                for (var g = 1; g <= groupCount; g++)
                {
                    var catchTotal = SafeGetPropertyWithIndex(fleetOutput, "CatchTotalByGroup", g);
                    var landings = SafeGetPropertyWithIndex(fleetInput, "Landings", g);
                    var discards = SafeGetPropertyWithIndex(fleetOutput, "DiscardByGroup", g);
                    var isNonZero = IsNonZero(catchTotal) || IsNonZero(landings) || IsNonZero(discards);
                    if (!isNonZero) continue;

                    var groupInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", g);
                    if (groupInput == null) continue;
                    catchByGroup.Add(new JObject
                    {
                        ["group_index"] = g,
                        ["group_name"] = SafeStringProperty(groupInput, "Name"),
                        ["catch_total"] = ToJToken(catchTotal),
                        ["landings"] = ToJToken(landings),
                        ["discards"] = ToJToken(discards)
                    });
                }

                fleets.Add(new JObject
                {
                    ["index"] = f,
                    ["name"] = SafeStringProperty(fleetInput, "Name"),
                    ["catch_by_group"] = catchByGroup
                });
            }

            return new JObject { ["fleets"] = fleets, ["total_count"] = fleetCount };
        }

        private JObject GetMtiMatrix()
        {
            var core = RequireCore();
            var nGroups = SafeIntProperty(core, "nGroups");
            var groupNames = new JArray();
            for (var i = 1; i <= nGroups; i++)
            {
                var input = SafeGetIndexedProperty(core, "EcopathGroupInputs", i);
                groupNames.Add(SafeStringProperty(input, "Name"));
            }

            // Try to read MTI from the Network Analysis plugin via PluginManager
            float[,] mtiData = null;
            try
            {
                var pluginManager = SafeGetProperty(core, "PluginManager");
                if (pluginManager != null)
                {
                    var getPluginsMethod = pluginManager.GetType().GetMethod("GetPlugins");
                    if (getPluginsMethod != null)
                    {
                        var iPluginType = getPluginsMethod.ReturnType.IsGenericType
                            ? getPluginsMethod.ReturnType.GetGenericArguments().FirstOrDefault()
                            : null;
                        if (iPluginType == null)
                        {
                            var asm = pluginManager.GetType().Assembly;
                            try
                            {
                                iPluginType = asm.GetTypes().FirstOrDefault(t => t.Name == "IPlugin");
                            }
                            catch (System.Reflection.ReflectionTypeLoadException rtle)
                            {
                                iPluginType = rtle.Types?.FirstOrDefault(t => t != null && t.Name == "IPlugin");
                            }
                        }
                        if (iPluginType != null)
                        {
                            var pluginCollection = getPluginsMethod.Invoke(pluginManager, new object[] { iPluginType });
                            if (pluginCollection is System.Collections.IEnumerable enumerable)
                            {
                                foreach (var plugin in enumerable)
                                {
                                    var pluginName = SafeStringProperty(plugin, "Name");
                                    if (pluginName.Contains("NetworkAnalysis") || pluginName.Contains("Network Analysis"))
                                    {
                                        var manager = SafeGetProperty(plugin, "Manager");
                                        if (manager != null)
                                        {
                                            var mtiProp = manager.GetType().GetProperty("MixedTrophicImpacts");
                                            if (mtiProp != null && mtiProp.GetIndexParameters().Length == 2)
                                            {
                                                mtiData = new float[nGroups + 1, nGroups + 1];
                                                for (var i = 1; i <= nGroups; i++)
                                                {
                                                    for (var j = 1; j <= nGroups; j++)
                                                    {
                                                        try
                                                        {
                                                            var val = mtiProp.GetValue(manager, new object[] { i, j });
                                                            if (val is float fval)
                                                                mtiData[i, j] = fval;
                                                            else if (val is double dval)
                                                                mtiData[i, j] = (float)dval;
                                                            else if (val != null)
                                                                float.TryParse(val.ToString(), out mtiData[i, j]);
                                                        }
                                                        catch { /* leave as zero */ }
                                                    }
                                                }
                                            }
                                            break; // Found the Network Analysis plugin
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            catch { /* MTI not available from plugin */ }

            // Build the matrix as JArray of JArrays
            var matrix = new JArray();
            for (var i = 1; i <= nGroups; i++)
            {
                var row = new JArray();
                for (var j = 1; j <= nGroups; j++)
                {
                    var val = (mtiData != null) ? mtiData[i, j] : 0.0f;
                    row.Add(Math.Round((double)val, 6));
                }
                matrix.Add(row);
            }

    var result = new JObject
    {
        ["status"] = (mtiData != null) ? "available" : "zero_fallback",
        ["group_names"] = groupNames,
        ["n_groups"] = nGroups,
        ["matrix"] = matrix
    };
    if (mtiData == null)
    {
        result["note"] = "MTI data unavailable. The Network Analysis plugin may not be installed or the Ecopath model may not have been balanced. A zero matrix is returned as fallback.";
    }
    return result;
        }

        private static string ToCsvValue(object val)
        {
            if (val == null) return "";
            if (val is float f) return f.ToString("G", System.Globalization.CultureInfo.InvariantCulture);
            if (val is double d) return d.ToString("G", System.Globalization.CultureInfo.InvariantCulture);
            return val.ToString();
        }

        private JObject ExportEcopathResults(JObject parameters)
        {
            var path = RequiredString(parameters, "path");
            if (string.IsNullOrWhiteSpace(path))
            {
                throw new EwEMcpException("missing_parameter", "path is required");
            }
            var ext = System.IO.Path.GetExtension(path).ToLowerInvariant();
            if (ext != ".csv" && ext != ".txt")
            {
                throw new EwEMcpException("invalid_parameter", "path must have a .csv or .txt extension");
            }
            // Check raw input for directory traversal before Path.GetFullPath resolves it away
            if (path.Contains(".."))
            {
                throw new EwEMcpException("invalid_parameter", "path must not contain directory traversal");
            }
            var fullPath = System.IO.Path.GetFullPath(path);
            var core = RequireCore();
            var sm = SafeGetProperty(core, "StateMonitor");
            bool hasRan = false;
            try { hasRan = SafeInvokeBool(sm, "HasEcopathRan"); } catch { }
            if (!hasRan)
            {
                throw new EwEMcpException("not_ready", "Ecopath has not been run; run it first before exporting results");
            }

            try
            {
                var writerType = core.GetType().Assembly.GetType("EwECore.cEcopathResultWriter");
                if (writerType != null)
                {
                    var writer = Activator.CreateInstance(writerType, core);
                    var writeResults = writerType.GetMethod("WriteResults", new[] { typeof(string) });
                    if (writeResults != null)
                    {
                        bool ok = (bool)writeResults.Invoke(writer, new object[] { path });
                        return new JObject
                        {
                            ["exported"] = ok,
                            ["path"] = path,
                            ["method"] = "result_writer"
                        };
                    }
                }
            }
            catch { }

            var count = SafeIntProperty(core, "nGroups");
            var sb = new System.Text.StringBuilder();
            sb.AppendLine("GroupNo,Group,B,PB,QB,EE,GE,FishMort,PredMort,BioAccum,NetMig,OtherMort,NatMort");
            for (var i = 1; i <= count; i++)
            {
                var input = SafeGetIndexedProperty(core, "EcopathGroupInputs", i);
                var output = SafeGetIndexedProperty(core, "EcopathGroupOutputs", i);
                if (input == null) continue;
                sb.Append(i);
                sb.Append(',');
                sb.Append((SafeStringProperty(input, "Name") ?? "").Replace(",", ";"));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "Biomass")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "PBOutput")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "QBOutput")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "EEOutput")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "GEOutput")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "MortCoFishRate")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "MortCoPredMort")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "BioAccum")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "NetMigration")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "MortCoOtherMort")));
                sb.Append(',');
                sb.Append(ToCsvValue(SafeGetProperty(output, "MortCoPB")));
                sb.AppendLine();
            }
            var csvContent = sb.ToString();
            System.Threading.Tasks.Task.Run(() =>
                System.IO.File.WriteAllText(fullPath, csvContent, System.Text.Encoding.UTF8)
            ).Wait();
            return new JObject
            {
                ["exported"] = true,
                ["path"] = path,
                ["method"] = "manual_csv",
                ["group_count"] = count
            };
        }

        private JObject RunEcosim()
        {
            bool ok = RequireCore().RunEcosim(null, true);
            return new JObject { ["started"] = ok, ["run_type"] = "ecosim" };
        }

        private JObject StopEcosim()
        {
            RequireCore().StopEcoSim();
            return new JObject { ["stopped"] = true, ["run_type"] = "ecosim" };
        }

        private JObject WaitForEcosimCompletion(JObject parameters)
        {
            var core = RequireCore();
            int timeoutSeconds = OptionalInt(parameters, "timeout_seconds") ?? 120;
            int pollIntervalMs = (OptionalInt(parameters, "poll_interval_seconds") ?? 1) * 1000;
            var sm = SafeGetProperty(core, "StateMonitor");
            var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
            while (DateTime.UtcNow < deadline)
            {
                if (SafeInvokeBool(sm, "HasEcosimRan"))
                {
                    return new JObject { ["completed"] = true, ["has_ecosim_ran"] = true };
                }
                if (SafeInvokeBool(sm, "HasEcosimInitialized"))
                {
                    // Still running
                }
                System.Threading.Thread.Sleep(pollIntervalMs);
            }
            throw new EwEMcpException("timeout", $"Ecosim did not complete within {timeoutSeconds}s");
        }

        private static readonly HashSet<string> BatchBlockedCommands = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "execute_ewe_code",
            "close_model",
            "load_model",
            "create_model",
            "save_model",
            "save_model_as",
            "run_ecopath",
            "run_ecosim",
            "stop_ecosim",
            "capture_view",
            "save_screenshot",
            "create_ecosim_scenario",
            "copy_ecosim_scenario",
            "delete_ecosim_scenario",
            "load_ecosim_scenario",
            "load_ecospace_scenario",
            "create_ecospace_scenario",
            "copy_ecospace_scenario",
            "delete_ecospace_scenario",
            "save_ecospace_scenario",
            "create_ecospace_mpa",
            "set_ecospace_mpa_cells",
            "create_regions_from_mpas",
            "run_ecospace",
            "stop_ecospace",
            "load_ecotracer_scenario",
            "create_ecotracer_scenario",
            "copy_ecotracer_scenario",
            "delete_ecotracer_scenario",
            "save_ecotracer_scenario",
            "close_ecotracer_scenario",
            "run_mse",
            "stop_mse",
            "run_monte_carlo",
            "stop_monte_carlo",
            "set_ecosim_forcing_shape",
            "export_ecopath_results",
            "create_ecopath_group",
            "create_ecopath_fleet",
            "shutdown_server",
            "invoke_ui_command",
            "import_time_series",
            "set_values",
            "set_indexed_value",
            "set_values_range",
            "export_ecosim_results",


        };
        private JObject GetBatchBlockedCommands()
        {
            return new JObject
            {
                ["commands"] = new JArray(BatchBlockedCommands.OrderBy(x => x))
            };
        }

        private JObject Batch(JObject parameters)
        {
            var commands = parameters?["commands"] as JArray ?? new JArray();
            var results = new JArray();
            foreach (var item in commands)
            {
                var type = item?["type"]?.ToString();
                if (BatchBlockedCommands.Contains(type ?? string.Empty))
                {
                    throw new EwEMcpException("blocked_batch_command", $"{type} is not allowed in batch");
                }
                var child = new EwEMcpRequest
                {
                    Id = Guid.NewGuid().ToString("N"),
                    Type = type,
                    Params = item?["params"] as JObject ?? new JObject(),
                    Token = _config.Token
                };
                results.Add(Dispatch(child));
            }
            return new JObject { ["results"] = results };
        }

        // ── View Capture ──────────────────────────────────────────────

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);

        private JObject CaptureView(JObject parameters)
        {
            var target = OptionalString(parameters, "target") ?? "auto";
            // Default to 1280x960 to prevent full-res 4K PNGs (~20 MB base64).
            // Callers can pass larger values or 0 to disable resizing.
            const int DefaultMaxWidth = 1280;
            const int DefaultMaxHeight = 960;
            var maxWidth = OptionalInt(parameters, "max_width") ?? DefaultMaxWidth;
            var maxHeight = OptionalInt(parameters, "max_height") ?? DefaultMaxHeight;

            Control captureTarget = ResolveCaptureTarget(target);
            if (captureTarget == null)
            {
                throw new EwEMcpException("capture_unavailable", "No EwE window is available for capture. Ensure EwE is running with a model loaded.");
            }

            string resolvedTarget = DescribeCaptureTarget(captureTarget);
            var size = captureTarget.Size;
            if (size.Width <= 0 || size.Height <= 0)
            {
                throw new EwEMcpException("capture_empty", $"Capture target has zero size ({size.Width}x{size.Height}).");
            }

            string base64;
            string captureMethod;
            bool wasResized;
            using (var bitmap = new Bitmap(size.Width, size.Height))
            {
                // Prefer PrintWindow for top-level forms — it captures even when obscured.
                // Fall back to DrawToBitmap for child controls or when PrintWindow fails.
                if (captureTarget is Form form && form.Handle != IntPtr.Zero && TryPrintWindow(form.Handle, bitmap))
                {
                    captureMethod = "PrintWindow";
                }
                else
                {
                    captureTarget.DrawToBitmap(bitmap, new Rectangle(Point.Empty, size));
                    captureMethod = "DrawToBitmap";
                }

                // Optional downscale to limit payload size.
                using (var finalBitmap = MaybeResize(bitmap, maxWidth, maxHeight))
                {
                    wasResized = finalBitmap != null;
                    base64 = BitmapToBase64Png(finalBitmap ?? bitmap);
                }
            }

            var active = GetActiveView();
            var result = new JObject
            {
                ["captured"] = true,
                ["target"] = resolvedTarget,
                ["capture_method"] = captureMethod,
                ["width"] = size.Width,
                ["height"] = size.Height,
                ["format"] = "png",
                ["encoding"] = "base64",
                ["data"] = base64,
                ["active_view"] = active
            };

            // Add final dimensions if resized.
            if (maxWidth > 0 || maxHeight > 0)
            {
                result["resized"] = wasResized;
            }

            return result;
        }

        private Control ResolveCaptureTarget(string target)
        {
            var formMain = SafeGetProperty(_uiContext(), "FormMain") as Control;
            var activeForm = Form.ActiveForm;

            switch ((target ?? "auto").Trim().ToLowerInvariant())
            {
                case "main":
                    return formMain;

                case "active":
                    return activeForm ?? formMain;

                case "content":
                    // Try to find the content panel inside the main form.
                    // EwE's FormMain typically uses a split container with a content area.
                    var content = FindContentPanel(formMain);
                    if (content != null && content.Width > 0 && content.Height > 0)
                    {
                        return content;
                    }
                    return formMain;

                case "auto":
                default:
                    // Default strategy: prefer a visible content panel, then active form, then main form.
                    var autoContent = FindContentPanel(formMain);
                    if (autoContent != null && autoContent.Width > 100 && autoContent.Height > 100)
                    {
                        return autoContent;
                    }
                    return activeForm ?? formMain;
            }
        }

        private static Control FindContentPanel(Control root)
        {
            if (root == null) return null;

            // Look for common content panel names and split containers.
            // EwE's main form uses SplitContainer layouts. The right panel (Panel2) typically
            // holds the content view.
            foreach (Control child in root.Controls)
            {
                if (child is SplitContainer split)
                {
                    // The right/bottom panel is usually the content area.
                    var panel2 = split.Panel2;
                    if (panel2 != null && panel2.Controls.Count > 0 && panel2.Width > 0 && panel2.Height > 0)
                    {
                        return panel2;
                    }
                }
            }

            // Recursive search for the first SplitContainer.
            foreach (Control child in root.Controls)
            {
                var found = FindContentPanel(child);
                if (found != null) return found;
            }

            return null;
        }

        private static string DescribeCaptureTarget(Control target)
        {
            if (target is Form form)
            {
                return $"form:{form.GetType().Name}";
            }
            if (target is SplitterPanel)
            {
                return "content_panel";
            }
            return $"control:{target.GetType().Name}";
        }

        private static bool TryPrintWindow(IntPtr hwnd, Bitmap bitmap)
        {
            try
            {
                using (var graphics = Graphics.FromImage(bitmap))
                {
                    IntPtr hdc = graphics.GetHdc();
                    try
                    {
                        // nFlags=2 means PW_RENDERFULLCONTENT (Windows 8.1+),
                        // falls back to 0 (PW_CLIENTONLY not set) on older Windows.
                        bool ok = PrintWindow(hwnd, hdc, 2);
                        if (!ok)
                        {
                            ok = PrintWindow(hwnd, hdc, 0);
                        }
                        return ok;
                    }
                    finally
                    {
                        graphics.ReleaseHdc(hdc);
                    }
                }
            }
            catch
            {
                return false;
            }
        }

        private static Bitmap MaybeResize(Bitmap source, int maxWidth, int maxHeight)
        {
            if (maxWidth <= 0 && maxHeight <= 0) return null;

            int targetWidth = source.Width;
            int targetHeight = source.Height;

            if (maxWidth > 0 && targetWidth > maxWidth)
            {
                double scale = (double)maxWidth / targetWidth;
                targetWidth = maxWidth;
                targetHeight = (int)(targetHeight * scale);
            }
            if (maxHeight > 0 && targetHeight > maxHeight)
            {
                double scale = (double)maxHeight / targetHeight;
                targetHeight = maxHeight;
                targetWidth = (int)(targetWidth * scale);
            }

            if (targetWidth == source.Width && targetHeight == source.Height) return null;
            if (targetWidth <= 0 || targetHeight <= 0) return null;

            var resized = new Bitmap(targetWidth, targetHeight);
            using (var g = Graphics.FromImage(resized))
            {
                g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                g.DrawImage(source, 0, 0, targetWidth, targetHeight);
            }
            return resized;
        }

        private static string BitmapToBase64Png(Bitmap bitmap)
        {
            using (var ms = new MemoryStream())
            {
                bitmap.Save(ms, ImageFormat.Png);
                return Convert.ToBase64String(ms.ToArray());
            }
        }



        // ── UI Refresh ────────────────────────────────────────────────

        private JObject RefreshUI()
        {
            var uic = _uiContext();
            if (uic == null) return new JObject { ["success"] = false, ["error"] = "UI context unavailable" };

            var methodsTried = new JArray();
            bool success = false;

            // Try refreshing the Main Form
            var formMain = SafeGetProperty(uic, "FormMain");
            if (formMain != null)
            {
                var refreshMethod = formMain.GetType().GetMethod("Refresh", BindingFlags.Instance | BindingFlags.Public, null, Type.EmptyTypes, null);
                if (refreshMethod != null)
                {
                    try
                    {
                        refreshMethod.Invoke(formMain, null);
                        methodsTried.Add("FormMain.Refresh() invoked");
                        success = true;
                    }
                    catch (Exception ex)
                    {
                        methodsTried.Add("FormMain.Refresh() failed: " + ex.Message);
                    }
                }
            }

            // Also try navigating to the current node if possible
            var cmdh = _commandHandler();
            if (cmdh != null)
            {
                var navigateCmd = cmdh.GetCommand("~navigate");
                if (navigateCmd != null)
                {
                    // Invoking navigate without a tag often refreshes the current view
                    try
                    {
                        navigateCmd.Invoke();
                        methodsTried.Add("~navigate invoked");
                        success = true;
                    }
                    catch { }
                }
            }

            return new JObject
            {
                ["success"] = success,
                ["methods_tried"] = methodsTried
            };
        }

        private JObject SetValues(JObject parameters)
        {
            var target = ResolveEntity(RequiredString(parameters, "domain"), OptionalInt(parameters, "index"));
            var values = parameters["values"] as JObject ?? throw new EwEMcpException("missing_parameter", "Missing 'values' object");
            var navigate = OptionalBool(parameters, "navigate", true);

            var result = SetGenericValues(target.NormalizedDomain, target.Entity, target.Index ?? 1, values);

            if (navigate)
            {
                var navigation = NavigateToSuggestedEditNode(target.NormalizedDomain, values.Properties().Select(p => p.Name));
                if (navigation != null)
                {
                    result["navigation"] = navigation;
                }
            }
            return result;
        }

        private JObject SetIndexedValue(JObject parameters)
        {
            var target = ResolveEntity(RequiredString(parameters, "domain"), OptionalInt(parameters, "index"));
            var variable = RequiredString(parameters, "variable");
            var indices = RequiredIntArray(parameters, "indices");
            var value = parameters["value"] ?? throw new EwEMcpException("missing_parameter", "Missing value");
            var navigation = OptionalBool(parameters, "navigate", true) ? NavigateToSuggestedEditNode(target.NormalizedDomain, new[] { variable }) : null;
            var property = FindIndexedProperty(target.Entity, variable, target.NormalizedDomain, indices.Length, requireWritable: true);
            if (property == null)
            {
                throw new EwEMcpException("unknown_or_readonly_indexed_variable", $"Indexed variable is not writable or not found: {variable}");
            }
            ValidateIndexedBounds(target, property, indices);
            if (!TryConvertValue(value, property.PropertyType, out var convertedValue))
            {
                throw new EwEMcpException("conversion_failed", $"Could not convert value to {FriendlyTypeName(property.PropertyType)}");
            }

            var rangeError = ValidatePropertyValue(target.Entity, property, convertedValue);
            if (rangeError != null)
            {
                throw new EwEMcpException("value_out_of_range", rangeError);
            }

            try
            {
                property.SetValue(target.Entity, convertedValue, indices.Cast<object>().ToArray());
                ForceValidateToDataSource(target.Entity, property.Name, convertedValue, target.NormalizedDomain);
                MarkDataSourceModified(target.NormalizedDomain);

                if (target.NormalizedDomain == "ecopath_groups" &&
                    string.Equals(property.Name, "DietComp", StringComparison.OrdinalIgnoreCase) &&
                    indices.Length >= 1)
                {
                    // Directly update the internal DC 2D array to ensure SaveEcopathDietComp picks it up.
                    var core = _core();
                    if (core != null)
                    {
                        // Find m_EcopathData.DC (primary computation data used by SaveEcopathDietComp)
                        try
                        {
                            var mDataField = core.GetType().GetField("m_EcopathData",
                                BindingFlags.Instance | BindingFlags.NonPublic);
                            var mData = mDataField?.GetValue(core);
                            EwEMcpFileLog.Info($"SetIndexedValue: m_EcopathData found={mData != null}");
                            if (mData != null)
                            {
                                var dcField = mData.GetType().GetField("DC",
                                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                                var dcArray = dcField?.GetValue(mData) as float[,];
                                EwEMcpFileLog.Info($"SetIndexedValue: DC array found={dcArray != null}, size=[{dcArray?.GetLength(0) ?? 0},{dcArray?.GetLength(1) ?? 0}]");
                                if (dcArray != null)
                                {
                                    // Resolve predator index from GroupDBID
                                    var dsData = SafeGetProperty(core, "EcopathDataStructures");
                                    if (dsData != null)
                                    {
                                        var groupDbIdField = dsData.GetType().GetField("GroupDBID",
                                            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                                        var groupDbIdArray = groupDbIdField?.GetValue(dsData) as Array;
                                        if (groupDbIdArray != null)
                                        {
                                            var predIdObj = SafeGetProperty(target.Entity, "GroupID") ?? SafeGetProperty(target.Entity, "DBID");
                                            if (predIdObj != null)
                                            {
                                                int predIdx = Array.IndexOf(groupDbIdArray, Convert.ToInt32(predIdObj));
                                                int preyIdx = indices[0];
                                                EwEMcpFileLog.Info($"SetIndexedValue: predIdx={predIdx} preyIdx={preyIdx} predId={predIdObj}");
                                                if (predIdx >= 1 && predIdx < dcArray.GetLength(0) && preyIdx < dcArray.GetLength(1))
                                                {
                                                    float dietVal = Convert.ToSingle(convertedValue);
                                                    dcArray[predIdx, preyIdx] = dietVal;
                                                    EwEMcpFileLog.Info($"SetIndexedValue: DC[{predIdx},{preyIdx}] = {dietVal}");
                                                }
                                                else
                                                    EwEMcpFileLog.Warn($"SetIndexedValue: bounds error [{predIdx},{preyIdx}] vs [{dcArray.GetLength(0)},{dcArray.GetLength(1)}]");
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        catch (Exception dex)
                        {
                            EwEMcpFileLog.Warn($"SetIndexedValue: DC update failed: {dex.Message}");
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                throw new EwEMcpException("set_failed", ex.Message);
            }

            // ── DietComp SQL persistence for new groups ──
            // Even when the in-memory DC array was updated above, SaveEcopathDietComp
            // (called during core.Save()) does DELETE+INSERT from the DC array, which
            // has zeros for newly created groups. Write directly to the database and
            // enqueue for post-save flush so the data survives load_model.
            // Uses 1-based group indices rather than GroupIDs because AddGroup assigns
            // temporary IDs that may differ from the DB-assigned GroupID after save/load.
            if (target.NormalizedDomain == "ecopath_groups" &&
                string.Equals(property.Name, "DietComp", StringComparison.OrdinalIgnoreCase) &&
                indices.Length >= 1 && target.Index.HasValue)
            {
                int predIndex = target.Index.Value;
                int preyIndex = indices[0];
                float dietVal = Convert.ToSingle(convertedValue);

                // Enqueue by index for post-save flush (IDs resolved at flush time).
                // Dictionary deduplicates by (PredIndex, PreyIndex) — repeated edits
                // to the same cell overwrite instead of appending.
                lock (_dietLock)
                {
                    _pendingDietWrites[(predIndex, preyIndex)] = dietVal;
                }

                // Immediate SQL write so get_ecopath_group / get_indexed_value
                // see the value (uses current GroupIDs which are correct for the
                // current in-memory state).
                var core2 = _core();
                if (core2 != null)
                {
                    try
                    {
                        var predInput = SafeGetIndexedProperty(core2, "EcopathGroupInputs", predIndex);
                        var preyInput = SafeGetIndexedProperty(core2, "EcopathGroupInputs", preyIndex);
                        if (predInput != null && preyInput != null)
                        {
                            int predId = Convert.ToInt32(
                                SafeGetProperty(predInput, "GroupID") ?? SafeGetProperty(predInput, "DBID"));
                            int preyId = Convert.ToInt32(
                                SafeGetProperty(preyInput, "GroupID") ?? SafeGetProperty(preyInput, "DBID"));

                            var connStr2 = GetConnectionString(core2);
                            if (!string.IsNullOrEmpty(connStr2))
                            {
                                using (var conn = new OleDbConnection(connStr2))
                                {
                                    conn.Open();
                                    using (var cmd = new OleDbCommand(
                                        "UPDATE [EcopathDietComp] SET [Diet] = @v WHERE [PredID] = @p1 AND [PreyID] = @p2",
                                        conn))
                                    {
                                        cmd.Parameters.AddWithValue("@v", dietVal);
                                        cmd.Parameters.AddWithValue("@p1", predId);
                                        cmd.Parameters.AddWithValue("@p2", preyId);
                                        int rows = cmd.ExecuteNonQuery();
                                        if (rows == 0)
                                        {
                                            using (var insertCmd = new OleDbCommand(
                                                "INSERT INTO [EcopathDietComp] ([PredID], [PreyID], [Diet]) VALUES (@p1, @p2, @v)",
                                                conn))
                                            {
                                                insertCmd.Parameters.AddWithValue("@p1", predId);
                                                insertCmd.Parameters.AddWithValue("@p2", preyId);
                                                insertCmd.Parameters.AddWithValue("@v", dietVal);
                                                insertCmd.ExecuteNonQuery();
                                            }
                                        }
                                    }
                                }
                                EwEMcpFileLog.Info($"SetIndexedValue: diet_comp SQL persisted (predIdx={predIndex}->preyIdx={preyIndex} = {dietVal})");
                            }
                        }
                    }
                    catch (Exception dex)
                    {
                        EwEMcpFileLog.Warn($"SetIndexedValue: diet_comp immediate SQL failed: {dex.GetType().Name}");
                    }
                }
            }

            var result = new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["variable"] = ToVariableName(property.Name),
                ["property"] = property.Name,
                ["indices"] = JArray.FromObject(indices),
                ["result"] = "updated",
                ["value"] = ToJToken(convertedValue)
            };
            if (navigation != null)
            {
                result["navigation"] = navigation;
            }
            return result;
        }



        private JObject SetGenericValues(string domain, object entity, int? index, JObject values)
        {
            if (entity == null)
            {
                throw new EwEMcpException("entity_unavailable", $"{domain} index {(index.HasValue ? index.Value.ToString() : "?")} is unavailable.");
            }

            var results = new JObject();
            foreach (var pair in values)
            {
        var property = FindWritableProperty(entity, pair.Key, domain);
        if (property == null)
        {
            // Check if the property exists but is indexed (requires
            // set_indexed_value instead of set_values).
            var indexedProp = FindAsIndexedProperty(entity, pair.Key, domain);
            if (indexedProp != null)
            {
                var indexParams = indexedProp.GetIndexParameters();
                var paramNames = string.Join(", ", indexParams.Select(p => p.Name));
                results[pair.Key] = new JObject
                {
                    ["status"] = "indexed_property",
                    ["message"] = $"Variable '{pair.Key}' is indexed ({indexParams.Length} index parameter(s): {paramNames}). Use set_indexed_value instead of set_values."
                };
            }
            else
            {
                results[pair.Key] = "unknown_or_readonly_property";
            }
            continue;
        }

                if (!TryConvertValue(pair.Value, property.PropertyType, out var convertedValue))
                {
                    results[pair.Key] = "conversion_failed";
                    continue;
                }

                var rangeError = ValidatePropertyValue(entity, property, convertedValue);
                if (rangeError != null)
                {
                    results[pair.Key] = new JObject { ["status"] = "value_out_of_range", ["reason"] = rangeError };
                    continue;
                }

                try
                {
                    property.SetValue(entity, convertedValue, null);
                    ForceValidateToDataSource(entity, property.Name, convertedValue, domain);

                    // ── DietComp bulk-array handling ──
                    // When diet_comp is set as a Single[] via set_values (not set_indexed_value),
                    // we must also update the internal DC float[,] array and write to SQL so
                    // save_model + load_model persist the values. Otherwise save_model reads
                    // the stale DC array and writes zeros.
                    if (domain == "ecopath_groups" &&
                        string.Equals(property.Name, "DietComp", StringComparison.OrdinalIgnoreCase) &&
                        pair.Value is JArray dietArray && index.HasValue)
                    {
                        int predIndex = index.Value;
                        var core = _core();
                        if (core != null)
                        {
                            try
                            {
                                // ── Shift to 1-based internal layout ──
                                // The Python client sends a 0-based array (index 0 = group 1),
                                // but EwE's entity stores DietComp[] as 1-based (index 1 = group 1).
                                // Shift all values right by one position so the entity, DC array,
                                // and SQL all see the correct prey indices.
                                // Internal storage needs an extra slot at index 0 (unused) so
                                // prey index N in the client maps to internal index N+1.
                                int nElements = dietArray.Count;
                                var dietShifted = new float[nElements + 1];
                                int nPrey = nElements;
                                for (int si = 1; si <= nElements; si++)
                                {
                                    float sv = Convert.ToSingle(dietArray[si - 1]);
                                    dietShifted[si] = sv;
                                }
                                // Apply the shifted array to the entity property
                                property.SetValue(entity, dietShifted, null);

                                // ── Update internal DC array ──
                                var mDataField = core.GetType().GetField("m_EcopathData",
                                    BindingFlags.Instance | BindingFlags.NonPublic);
                                var mData = mDataField?.GetValue(core);
                                if (mData != null)
                                {
                                    var dcField = mData.GetType().GetField("DC",
                                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                                    var dcArray = dcField?.GetValue(mData) as float[,];
                                    if (dcArray != null)
                                    {
                                        var dsData = SafeGetProperty(core, "EcopathDataStructures");
                                        if (dsData != null)
                                        {
                                            var groupDbIdField = dsData.GetType().GetField("GroupDBID",
                                                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                                            var groupDbIdArray = groupDbIdField?.GetValue(dsData) as Array;
                                            if (groupDbIdArray != null)
                                            {
                                                var predIdObj = SafeGetProperty(entity, "GroupID") ?? SafeGetProperty(entity, "DBID");
                                                if (predIdObj != null)
                                                {
                                                    int predIdx = Array.IndexOf(groupDbIdArray, Convert.ToInt32(predIdObj));
                                                    if (predIdx >= 1 && predIdx < dcArray.GetLength(0))
                                                    {
                                                        nPrey = Math.Min(nElements, dcArray.GetLength(1) - 1);
                                                        for (int prey = 1; prey <= nPrey; prey++)
                                                        {
                                                            dcArray[predIdx, prey] = dietShifted[prey];
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }

                                // ── Enqueue pending writes (inside lock — dictionary only) ──
                                lock (_dietLock)
                                {
                                    for (int preyIdx = 1; preyIdx <= nPrey; preyIdx++)
                                    {
                                        _pendingDietWrites[(predIndex, preyIdx)] = dietShifted[preyIdx];
                                    }
                                }

                                // ── Immediate SQL writes (outside lock — may do I/O) ──
                                var connStr = GetConnectionString(core);
                                var predInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", predIndex);
                                int predGroupId = 0;
                                if (predInput != null)
                                    predGroupId = Convert.ToInt32(
                                        SafeGetProperty(predInput, "GroupID") ?? SafeGetProperty(predInput, "DBID"));

                                if (!string.IsNullOrEmpty(connStr) && predGroupId > 0)
                                {
                                    OleDbConnection sqlConn = null;
                                    try
                                    {
                                        sqlConn = new OleDbConnection(connStr);
                                        sqlConn.Open();
                                        for (int preyIdx = 1; preyIdx <= nPrey; preyIdx++)
                                        {
                                            float val = dietShifted[preyIdx];
                                            int preyGroupId = 0;
                                            var preyInput = SafeGetIndexedProperty(core, "EcopathGroupInputs", preyIdx);
                                            if (preyInput != null)
                                            {
                                                preyGroupId = Convert.ToInt32(
                                                    SafeGetProperty(preyInput, "GroupID") ?? SafeGetProperty(preyInput, "DBID"));
                                            }
                                            if (preyGroupId <= 0)
                                            {
                                                EwEMcpFileLog.Warn($"SetGenericValues: diet_comp skip SQL for preyIdx={preyIdx}, could not resolve prey GroupID");
                                                continue;
                                            }

                                            using (var cmd = new OleDbCommand(
                                                "UPDATE [EcopathDietComp] SET [Diet] = @v WHERE [PredID] = @p1 AND [PreyID] = @p2",
                                                sqlConn))
                                            {
                                                cmd.Parameters.AddWithValue("@v", val);
                                                cmd.Parameters.AddWithValue("@p1", predGroupId);
                                                cmd.Parameters.AddWithValue("@p2", preyGroupId);
                                                int rows = cmd.ExecuteNonQuery();
                                                if (rows == 0)
                                                {
                                                    using (var insertCmd = new OleDbCommand(
                                                        "INSERT INTO [EcopathDietComp] ([PredID], [PreyID], [Diet]) VALUES (@p1, @p2, @v)",
                                                        sqlConn))
                                                    {
                                                        insertCmd.Parameters.AddWithValue("@p1", predGroupId);
                                                        insertCmd.Parameters.AddWithValue("@p2", preyGroupId);
                                                        insertCmd.Parameters.AddWithValue("@v", val);
                                                        insertCmd.ExecuteNonQuery();
                                                    }
                                                }
                                            }
                                        }
                                        sqlConn.Close();
                                    }
                                    catch (Exception sqlex)
                                    {
                                        try { sqlConn?.Close(); } catch { }
                                        EwEMcpFileLog.Warn($"SetGenericValues: diet_comp SQL failed: {sqlex.GetType().Name}");
                                    }
                                    finally
                                    {
                                        sqlConn?.Dispose();
                                    }
                                }
                            }
                            catch (Exception dex)
                            {
                                EwEMcpFileLog.Warn($"SetGenericValues: diet_comp bulk handling failed: {dex.Message}");
                            }
                        }
                    }

                    results[pair.Key] = "updated";
                }
                catch
                {
                    results[pair.Key] = "failed_to_set";
                }
            }

            return new JObject
            {
                ["domain"] = domain,
                ["index"] = index.HasValue ? JToken.FromObject(index.Value) : JValue.CreateNull(),
                ["results"] = results
            };
        }

private static string ValidatePropertyValue(object entity, PropertyInfo property, object value)
{
    if (value == null || !IsNumericType(value.GetType())) return null;

    double numericValue = Convert.ToDouble(value);
    var metadata = ResolveVariableMetadata(entity, property);
    if (metadata == null) return null;

    var minObj = SafeGetProperty(metadata, "Min");
    var maxObj = SafeGetProperty(metadata, "Max");
    if (minObj == null && maxObj == null) return null;

    double min = minObj != null ? Convert.ToDouble(minObj) : double.MinValue;
    double max = maxObj != null ? Convert.ToDouble(maxObj) : double.MaxValue;

    bool minExclusive = IsOperatorExclusive(SafeGetProperty(metadata, "MinOperator"), isMin: true);
    bool maxExclusive = IsOperatorExclusive(SafeGetProperty(metadata, "MaxOperator"), isMin: false);

    bool violatesMin = minExclusive ? numericValue <= min : numericValue < min;
    bool violatesMax = maxExclusive ? numericValue >= max : numericValue > max;

    if (violatesMin || violatesMax)
    {
        string minDesc = minExclusive ? $"> {min}" : $">= {min}";
        string maxDesc = maxExclusive ? $"< {max}" : $"<= {max}";
        return $"Value {numericValue} is outside valid range ({minDesc}, {maxDesc})";
    }

    return null;
}

/// <summary>
/// Map a C# property name (e.g. "EEInput", "DenDepCatchability") to an
/// eVarNameFlags enum value by matching the property name against enum member names.
/// </summary>
private static object MapPropertyNameToVarNameFlag(string propertyName, Type enumType)
{
    if (string.IsNullOrEmpty(propertyName) || enumType == null || !enumType.IsEnum) return null;

    // Direct match: property name == enum member name
    if (Enum.IsDefined(enumType, propertyName))
    {
        return Enum.Parse(enumType, propertyName);
    }

    // Status suffix: "EEInputStatus" -> try "EEInput"
    if (propertyName.EndsWith("Status") && propertyName.Length > 6)
    {
        var baseName = propertyName.Substring(0, propertyName.Length - 6);
        if (Enum.IsDefined(enumType, baseName))
        {
            return Enum.Parse(enumType, baseName);
        }
    }

    // Try case-insensitive match
    foreach (var name in Enum.GetNames(enumType))
    {
        if (string.Equals(name, propertyName, StringComparison.OrdinalIgnoreCase))
        {
            return Enum.Parse(enumType, name);
        }
    }

    return null;
}

 /// <summary>
 /// Determine if a Min/Max operator is exclusive (strict comparison).
 /// For MinOperator: GreaterThan (= exclusive, value must be > min)
 /// For MaxOperator: LessThan (= exclusive, value must be < max)
 /// Returns true if the operator enforces strict (exclusive) comparison.
 /// </summary>
 private static bool IsOperatorExclusive(object operatorObj, bool isMin)
 {
 if (operatorObj == null) return true; // Default to exclusive if operator missing
 var typeName = operatorObj.GetType().Name;
 if (isMin)
 {
 // MinOperator: GreaterThan means exclusive (value > min), GreaterThanOrEqualTo means inclusive (value >= min)
 return typeName == "cGreaterThan";
 }
 else
 {
 // MaxOperator: LessThan means exclusive (value < max), LessThanOrEqualTo means inclusive (value <= max)
 return typeName == "cLessThan";
 }
 }

        private static bool IsNumericType(Type type)
        {
            var t = Nullable.GetUnderlyingType(type) ?? type;
            return t == typeof(int) || t == typeof(long) || t == typeof(float) || t == typeof(double) || t == typeof(decimal) || t == typeof(short) || t == typeof(byte) || t == typeof(uint) || t == typeof(ulong) || t == typeof(ushort) || t == typeof(sbyte);
        }

        private EntityTarget ResolveEntity(string domain, int? requestedIndex)
        {
            var core = RequireCore();
            var normalized = NormalizeDomain(domain);
            if (normalized == "core")
            {
                return new EntityTarget(normalized, core, null);
            }

            if (normalized == "ecopath_stats")
            {
                var statsIndex = requestedIndex ?? 1;
                if (statsIndex != 1)
                {
                    throw new EwEMcpException("index_out_of_range", $"{normalized} index {statsIndex} is outside 1..1");
                }

                var stats = SafeGetProperty(core, "EcopathStats");
                if (stats == null)
                {
                    throw new EwEMcpException("entity_unavailable", $"Could not resolve {normalized} via EcopathStats");
                }

                return new EntityTarget(normalized, stats, null);
            }

            var count = GetDomainCount(core, normalized);
            var index = requestedIndex ?? 1;
            if (index < 1 || index > count)
            {
                throw new EwEMcpException("index_out_of_range", $"{normalized} index {index} is outside 1..{count}");
            }

            var propertyName = GetDomainIndexedPropertyName(normalized);
            var entity = SafeGetIndexedProperty(core, propertyName, index);
            if (entity == null)
            {
                throw new EwEMcpException("entity_unavailable", $"Could not resolve {normalized}[{index}] via {propertyName}");
            }

            return new EntityTarget(normalized, entity, index);
        }

        private static string NormalizeDomain(string domain)
        {
            switch ((domain ?? string.Empty).Trim().ToLowerInvariant())
            {
                case "core":
                    return "core";
                case "ecopath_group":
                case "ecopath_groups":
                    return "ecopath_groups";
                case "ecopath_group_output":
                case "ecopath_group_outputs":
                    return "ecopath_group_outputs";
                case "ecopath_fleet":
                case "ecopath_fleets":
                    return "ecopath_fleets";
                case "ecopath_fleet_output":
                case "ecopath_fleet_outputs":
                    return "ecopath_fleet_outputs";
                case "ecopath_stat":
                case "ecopath_stats":
                    return "ecopath_stats";
                case "ecosim_scenario":
                case "ecosim_scenarios":
                    return "ecosim_scenarios";
                case "ecosim_group_input":
                case "ecosim_group_inputs":
                    return "ecosim_group_inputs";
                case "ecosim_group_output":
                case "ecosim_group_outputs":
                    return "ecosim_group_outputs";
                case "ecosim_fleet_input":
                case "ecosim_fleet_inputs":
                    return "ecosim_fleet_inputs";
                case "ecosim_fleet_output":
                case "ecosim_fleet_outputs":
                    return "ecosim_fleet_outputs";
                case "ecospace_scenario":
                case "ecospace_scenarios":
                    return "ecospace_scenarios";
                case "ecospace_group_input":
                case "ecospace_group_inputs":
                    return "ecospace_group_inputs";
                case "ecospace_fleet_input":
                case "ecospace_fleet_inputs":
                    return "ecospace_fleet_inputs";
                case "ecospace_group_output":
                case "ecospace_group_outputs":
                    return "ecospace_group_outputs";
                case "ecospace_fleet_output":
                case "ecospace_fleet_outputs":
                    return "ecospace_fleet_outputs";
                case "ecospace_habitat":
                case "ecospace_habitats":
                    return "ecospace_habitats";
                case "ecospace_mpa":
                case "ecospace_mpas":
                    return "ecospace_mpas";
                case "ecospace_region_output":
                case "ecospace_region_outputs":
                    return "ecospace_region_outputs";
                case "ecotracer_scenario":
                case "ecotracer_scenarios":
                    return "ecotracer_scenarios";
                case "ecotracer_group_input":
                case "ecotracer_group_inputs":
                    return "ecotracer_group_inputs";
                case "ecotracer_model_parameter":
                case "ecotracer_model_parameters":
                    return "ecotracer_model_parameters";
                case "ecotracer_group_output":
                case "ecotracer_group_outputs":
                    return "ecotracer_group_outputs";
                case "mse_parameter":
                case "mse_parameters":
                    return "mse_parameters";
                case "mse_group_input":
                case "mse_group_inputs":
                    return "mse_group_inputs";
                case "mse_fleet_input":
                case "mse_fleet_inputs":
                    return "mse_fleet_inputs";
                case "mse_group_output":
                case "mse_group_outputs":
                    return "mse_group_outputs";
                case "mse_fleet_output":
                case "mse_fleet_outputs":
                    return "mse_fleet_outputs";
                case "monte_carlo_group":
                case "monte_carlo_groups":
                    return "monte_carlo_groups";
                case "taxon":
                case "taxa":
                    return "taxa";
                default:
                    throw new EwEMcpException("unknown_domain", $"Unknown domain: {domain}");
            }
        }

        private static int GetDomainCount(cCore core, string normalizedDomain)
        {
            switch (normalizedDomain)
            {
                case "ecopath_groups":
                case "ecopath_group_outputs":
                    return SafeIntProperty(core, "nGroups");
                case "ecopath_stats":
                    return 1;
                case "ecopath_fleets":
                case "ecopath_fleet_outputs":
                case "ecosim_fleet_inputs":
                case "ecosim_fleet_outputs":
                case "ecospace_fleet_inputs":
                case "ecospace_fleet_outputs":
                    return SafeIntProperty(core, "nFleets");
                case "ecosim_scenarios":
                    return SafeIntProperty(core, "nEcosimScenarios");
                case "ecosim_group_inputs":
                case "ecosim_group_outputs":
                case "ecospace_group_inputs":
                case "ecospace_group_outputs":
                case "ecotracer_group_inputs":
                    return SafeIntProperty(core, "nGroups");
                case "ecospace_scenarios":
                    return SafeIntProperty(core, "nEcospaceScenarios");
                case "ecospace_habitats":
                    return SafeIntProperty(core, "nHabitats");
                case "ecospace_mpas":
                    return SafeIntProperty(core, "nMPAs");
                case "ecospace_region_outputs":
                    return SafeIntProperty(core, "nRegions");
                case "ecotracer_scenarios":
                    return SafeIntProperty(core, "nEcotracerScenarios");
                case "ecotracer_model_parameters":
                    return 1;
                case "ecotracer_group_outputs":
                    return SafeIntProperty(core, "nGroups");
                case "mse_parameters":
                    return 1;
                case "mse_group_inputs":
                case "mse_group_outputs":
                    return SafeIntProperty(core, "nGroups");
                case "mse_fleet_inputs":
                case "mse_fleet_outputs":
                    return SafeIntProperty(core, "nFleets");
                case "monte_carlo_groups":
                    return SafeIntProperty(core, "nGroups");
                case "taxa":
                    return SafeIntProperty(core, "nTaxon");
                default:
                    throw new EwEMcpException("unknown_domain", $"Unknown indexed domain: {normalizedDomain}");
            }
        }

        private static string GetDomainIndexedPropertyName(string normalizedDomain)
        {
            switch (normalizedDomain)
            {
                case "ecopath_groups":
                    return "EcopathGroupInputs";
                case "ecopath_group_outputs":
                    return "EcopathGroupOutputs";
                case "ecopath_fleets":
                    return "EcopathFleetInputs";
                case "ecopath_fleet_outputs":
                    return "EcopathFleetOutputs";
                case "ecopath_stats":
                    return "EcopathStats";
                case "ecosim_scenarios":
                    return "EcosimScenarios";
                case "ecosim_group_inputs":
                    return "EcosimGroupInputs";
                case "ecosim_group_outputs":
                    return "EcosimGroupOutputs";
                case "ecosim_fleet_inputs":
                    return "EcosimFleetInputs";
                case "ecosim_fleet_outputs":
                    return "EcosimFleetOutput";
                case "ecospace_scenarios":
                    return "EcospaceScenarios";
                case "ecospace_group_inputs":
                    return "EcospaceGroupInputs";
                case "ecospace_fleet_inputs":
                    return "EcospaceFleetInputs";
                case "ecospace_group_outputs":
                    return "EcospaceGroupOutput";
                case "ecospace_fleet_outputs":
                    return "EcospaceFleetOutput";
                case "ecospace_habitats":
                    return "EcospaceHabitats";
                case "ecospace_mpas":
                    return "EcospaceMPAs";
                case "ecospace_region_outputs":
                    return "EcospaceRegionOutput";
                case "ecotracer_scenarios":
                    return "EcotracerScenarios";
                case "ecotracer_group_inputs":
                    return "EcotracerGroupInputs";
                case "ecotracer_model_parameters":
                    return "EcotracerModelParameters";
                case "ecotracer_group_outputs":
                    return "EcotracerGroupResults";
                case "mse_parameters":
                    return "MSEManager.ModelParameters";
                case "mse_group_inputs":
                    return "MSEManager.GroupInputs";
                case "mse_fleet_inputs":
                    return "MSEManager.EcopathFleetInputs";
                case "mse_group_outputs":
                    return "MSEManager.GroupOutputs";
                case "mse_fleet_outputs":
                    return "MSEManager.FleetOutputs";
                case "monte_carlo_groups":
                    return "EcosimMonteCarlo.Groups";
                case "taxa":
                    return "Taxon";
                default:
                    throw new EwEMcpException("unknown_domain", $"Unknown indexed domain: {normalizedDomain}");
            }
        }

        private JObject ReadScalarValues(object entity, string[] requestedVariables, bool includeErrors, string domain)
        {
            var result = new JObject();
            if (entity == null)
            {
                return result;
            }

            if (requestedVariables != null && requestedVariables.Length > 0)
            {
                foreach (var variable in requestedVariables)
                {
            var property = FindReadableProperty(entity, variable, domain);
            if (property == null)
            {
                if (includeErrors)
                {
                    // Check if the property exists but is indexed (requires
                    // get_indexed_value / set_indexed_value instead).
                    var indexedProp = FindAsIndexedProperty(entity, variable, domain);
                    if (indexedProp != null)
                    {
                        var indexParams = indexedProp.GetIndexParameters();
                        var paramNames = string.Join(", ", indexParams.Select(p => p.Name));
                        result[variable] = new JObject
                        {
                            ["error"] = "indexed_variable",
                            ["message"] = $"Variable '{variable}' is indexed ({indexParams.Length} index parameter(s): {paramNames}). Use get_indexed_value or set_indexed_value instead of get_values or set_values."
                        };
                    }
                    else
                    {
                        result[variable] = new JObject { ["error"] = "unknown_variable" };
                    }
                }
                continue;
            }
            result[variable] = ReadPropertyValue(entity, property);
                }
                return result;
            }

            foreach (var property in GetReadableProperties(entity)
                         .Where(p => p.GetIndexParameters().Length == 0 && IsValueLikeType(p.PropertyType)))
            {
                result[ToVariableName(property.Name)] = ReadPropertyValue(entity, property);
            }
            return result;
        }

        private static JToken ReadPropertyValue(object entity, PropertyInfo property)
        {
            try
            {
                var value = property.GetValue(entity, null);

                // Shift DietComp from 1-based internal layout back to 0-based for
                // the Python client. The internal array stores diet[1] = group 1 value
                // (index 0 is unused). The Python client expects diet[0] = group 1 value
                // with no trailing/unused slot.
                if (string.Equals(property.Name, "DietComp", StringComparison.OrdinalIgnoreCase) &&
                    value is float[] dietInternal && dietInternal.Length > 1)
                {
                    var dietClient = new float[dietInternal.Length - 1];
                    for (int i = 1; i < dietInternal.Length; i++)
                        dietClient[i - 1] = dietInternal[i];
                    return ToJToken(dietClient);
                }

                return ToJToken(value);
            }
            catch (Exception ex)
            {
                return new JObject
                {
                    ["error"] = "read_failed",
                    ["message"] = ex.Message
                };
            }
        }

        private static JToken ReadIndexedPropertyValue(object entity, PropertyInfo property, int[] indices)
        {
            try
            {
                return ToJToken(property.GetValue(entity, indices.Cast<object>().ToArray()));
            }
            catch (Exception ex)
            {
                throw new EwEMcpException("indexed_read_failed", ex.InnerException?.Message ?? ex.Message);
            }
        }

        private static IEnumerable<PropertyInfo> GetReadableProperties(object entity)
        {
            if (entity == null)
            {
                return Enumerable.Empty<PropertyInfo>();
            }

            return entity.GetType()
                .GetProperties(BindingFlags.Instance | BindingFlags.Public)
                .Where(p => p.CanRead)
                .OrderBy(p => p.Name)
                .ThenBy(p => p.GetIndexParameters().Length);
        }

private static JObject PropertyMetadata(PropertyInfo property, string domain, object entity = null)
{
    var indexParameters = property.GetIndexParameters();
    var result = new JObject
    {
        ["name"] = ToVariableName(property.Name),
        ["property"] = property.Name,
        ["domain"] = domain ?? string.Empty,
        ["type"] = FriendlyTypeName(property.PropertyType),
        ["readable"] = property.CanRead,
        ["writable"] = property.CanWrite && !IsReadOnlyDomain(domain),
        ["indexed"] = indexParameters.Length > 0,
        ["index_count"] = indexParameters.Length
    };

    // Enrich with range metadata if available
    if (entity != null && IsNumericType(property.PropertyType))
    {
        try
        {
            object metadata = ResolveVariableMetadata(entity, property);
            if (metadata != null)
            {
                var minObj = SafeGetProperty(metadata, "Min");
                var maxObj = SafeGetProperty(metadata, "Max");
                if (minObj != null) result["min"] = Convert.ToDouble(minObj);
                if (maxObj != null) result["max"] = Convert.ToDouble(maxObj);
                var minOp = SafeGetProperty(metadata, "MinOperator");
                var maxOp = SafeGetProperty(metadata, "MaxOperator");
                if (minOp != null) result["min_operator"] = minOp.GetType().Name;
                if (maxOp != null) result["max_operator"] = maxOp.GetType().Name;
            }
        }
        catch { }
    }

    return result;
}

/// <summary>
/// Resolve variable metadata from an EwE entity by trying entity.Metadata,
/// GetVariableMetadata(eVarNameFlags), and the static cVariableMetaData repository.
/// </summary>
private static object ResolveVariableMetadata(object entity, PropertyInfo property)
{
    // Path 1: entity.Metadata
    try
    {
        var metadata = SafeGetProperty(entity, "Metadata");
        if (metadata != null)
        {
            var minObj = SafeGetProperty(metadata, "Min");
            var maxObj = SafeGetProperty(metadata, "Max");
            if (minObj != null || maxObj != null) return metadata;
        }
    }
    catch { }

    // Path 2: GetVariableMetadata(eVarNameFlags) via reflection
    try
    {
        var getMetaMethod = entity.GetType().GetMethod("GetVariableMetadata");
        if (getMetaMethod != null)
        {
            var parameters = getMetaMethod.GetParameters();
            if (parameters.Length == 1)
            {
                var enumType = parameters[0].ParameterType;
                var enumValue = MapPropertyNameToVarNameFlag(property.Name, enumType);
                if (enumValue != null)
                {
                    var metadata = getMetaMethod.Invoke(entity, new object[] { enumValue });
                    if (metadata != null) return metadata;
                }
            }
        }
    }
    catch { }

    // Path 3: Static cVariableMetaData.Get(eVarNameFlags) repository
    try
    {
        var getMetaMethod = entity.GetType().GetMethod("GetVariableMetadata");
        if (getMetaMethod != null)
        {
            var parameters = getMetaMethod.GetParameters();
            if (parameters.Length == 1)
            {
                var enumType = parameters[0].ParameterType;
                var enumValue = MapPropertyNameToVarNameFlag(property.Name, enumType);
                if (enumValue != null)
                {
                    foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                    {
                        try
                        {
                            var staticMetaType = asm.GetType("EwECore.cVariableMetaData");
                            if (staticMetaType != null)
                            {
                                var staticGetMethod = staticMetaType.GetMethod("Get", new[] { enumType });
                                if (staticGetMethod != null)
                                {
                                    var metadata = staticGetMethod.Invoke(null, new object[] { enumValue });
                                    if (metadata != null) return metadata;
                                }
                            }
                        }
                        catch { }
                    }
                }
            }
        }
    }
    catch { }

    return null;
}

private static PropertyInfo FindReadableProperty(object entity, string variable, string domain)
    {
        if (entity == null)
        {
            return null;
        }

        var alias = AliasPropertyName(variable, domain);
        return GetReadableProperties(entity)
            .Where(p => p.GetIndexParameters().Length == 0)
            .FirstOrDefault(p =>
                string.Equals(p.Name, alias, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(NormalizeVariableName(p.Name), NormalizeVariableName(alias), StringComparison.OrdinalIgnoreCase));
    }

    private static PropertyInfo FindWritableProperty(object entity, string variable, string domain)
    {
        if (IsReadOnlyDomain(domain))
        {
            return null;
        }

        var alias = AliasPropertyName(variable, domain);
        return GetReadableProperties(entity)
            .Where(p => p.CanWrite && p.GetIndexParameters().Length == 0)
            .FirstOrDefault(p =>
                string.Equals(p.Name, alias, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(NormalizeVariableName(p.Name), NormalizeVariableName(alias), StringComparison.OrdinalIgnoreCase));
    }

    /// <summary>
    /// Check whether a named property exists on the entity but is indexed
    /// (i.e., requires get_indexed_value / set_indexed_value instead of
    /// get_values / set_values). Returns the matching indexed PropertyInfo
    /// or null if no match or the property is scalar.
    /// </summary>
    private static PropertyInfo FindAsIndexedProperty(object entity, string variable, string domain)
    {
        if (entity == null)
        {
            return null;
        }

        var alias = AliasPropertyName(variable, domain);
        return GetReadableProperties(entity)
            .Where(p => p.GetIndexParameters().Length > 0)
            .FirstOrDefault(p =>
                string.Equals(p.Name, alias, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(NormalizeVariableName(p.Name), NormalizeVariableName(alias), StringComparison.OrdinalIgnoreCase));
    }

        private static PropertyInfo FindIndexedProperty(object entity, string variable, string domain, int indexCount, bool requireWritable)
        {
            if (entity == null)
            {
                return null;
            }

            if (requireWritable && IsReadOnlyDomain(domain))
            {
                return null;
            }

            var alias = AliasPropertyName(variable, domain);
            return GetReadableProperties(entity)
                .Where(p => p.GetIndexParameters().Length == indexCount)
                .Where(p => !requireWritable || p.CanWrite)
                .FirstOrDefault(p =>
                    string.Equals(p.Name, alias, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(NormalizeVariableName(p.Name), NormalizeVariableName(alias), StringComparison.OrdinalIgnoreCase));
        }

        private static bool IsReadOnlyDomain(string domain)
        {
            switch ((domain ?? string.Empty).Trim().ToLowerInvariant())
            {
                case "ecopath_group_outputs":
                case "ecopath_fleet_outputs":
                case "ecopath_stats":
                case "ecosim_group_outputs":
                case "ecosim_fleet_outputs":
                case "ecospace_group_outputs":
                case "ecospace_fleet_outputs":
                case "ecospace_region_outputs":
                case "ecotracer_group_outputs":
                case "mse_group_outputs":
                case "mse_fleet_outputs":
                    return true;
                default:
                    return false;
            }
        }

        private static JObject IndexedValidationResult(EntityTarget target, string variable, int[] indices, bool valid, string reason, PropertyInfo property)
        {
            var result = new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["variable"] = property == null ? variable : ToVariableName(property.Name),
                ["indices"] = JArray.FromObject(indices),
                ["valid"] = valid
            };
            if (property != null)
            {
                result["property"] = property.Name;
                result["type"] = FriendlyTypeName(property.PropertyType);
            }
            if (!string.IsNullOrWhiteSpace(reason))
            {
                result["reason"] = reason;
            }
            return result;
        }

        private void ValidateIndexedBounds(EntityTarget target, PropertyInfo property, int[] indices)
        {
            var reason = GetIndexedBoundsError(target, property, indices);
            if (!string.IsNullOrWhiteSpace(reason))
            {
                throw new EwEMcpException("indexed_index_out_of_range", reason);
            }
        }

        private string GetIndexedBoundsError(EntityTarget target, PropertyInfo property, int[] indices)
        {
            if (target == null || property == null || indices == null || indices.Length != 1)
            {
                return string.Empty;
            }

            var propertyName = property.Name ?? string.Empty;
            var boundedByGroups =
                (target.NormalizedDomain == "ecopath_groups" && string.Equals(propertyName, "DietComp", StringComparison.OrdinalIgnoreCase)) ||
                (target.NormalizedDomain == "ecopath_fleets" &&
                    (string.Equals(propertyName, "Landings", StringComparison.OrdinalIgnoreCase) ||
                     string.Equals(propertyName, "Discards", StringComparison.OrdinalIgnoreCase)));

            if (!boundedByGroups)
            {
                return string.Empty;
            }

            var groupCount = SafeIntProperty(RequireCore(), "nGroups");
            if (indices[0] < 1 || indices[0] > groupCount)
            {
                return $"Index {indices[0]} is outside valid group range 1..{groupCount} for {propertyName}";
            }

            return string.Empty;
        }

        private static string AliasPropertyName(string variable, string domain)
        {
            var key = (variable ?? string.Empty).Trim().ToLowerInvariant();
            switch (key)
            {
                case "biomass_area":
                    return domain == "ecopath_group_outputs" ? "BiomassArea" : "BiomassAreaInput";
                case "pb":
                    return "PBInput";
                case "qb":
                    return "QBInput";
                case "ee":
                    return "EEInput";
                case "name":
                    return "Name";
                default:
                    return variable;
            }
        }

        private static string ToVariableName(string propertyName)
        {
            if (string.IsNullOrWhiteSpace(propertyName))
            {
                return string.Empty;
            }

            var chars = new List<char>();
            for (var i = 0; i < propertyName.Length; i++)
            {
                var c = propertyName[i];
                if (i > 0 && char.IsUpper(c) && (char.IsLower(propertyName[i - 1]) || char.IsDigit(propertyName[i - 1])))
                {
                    chars.Add('_');
                }
                chars.Add(char.ToLowerInvariant(c));
            }
            return new string(chars.ToArray());
        }

        private static string NormalizeVariableName(string value)
        {
            return new string((value ?? string.Empty)
                .Where(char.IsLetterOrDigit)
                .Select(char.ToLowerInvariant)
                .ToArray());
        }

        private static bool IsValueLikeType(Type type)
        {
            if (type == null)
            {
                return false;
            }
            // Arrays are never "scalar-like" — they blow up ReadScalarValues payloads
            // when requestedVariables is null (the "dump all" path). Array-returning
            // properties (e.g., Single[] PredMort on cEcopathGroupOutput) must be read
            // explicitly via get_values or get_indexed_value instead.
            if (type.IsArray)
            {
                return false;
            }
            var t = Nullable.GetUnderlyingType(type) ?? type;
            return t.IsPrimitive || t.IsEnum || t == typeof(string) || t == typeof(decimal) || t == typeof(DateTime);
        }

        private static string FriendlyTypeName(Type type)
        {
            if (type == null)
            {
                return string.Empty;
            }
            if (type.IsArray)
            {
                return $"{FriendlyTypeName(type.GetElementType())}[]";
            }
            return (Nullable.GetUnderlyingType(type) ?? type).Name;
        }

        private JObject ShutdownServer()
        {
            if (!_config.EnableEval)
            {
                throw new EwEMcpException("shutdown_disabled", "shutdown_server is disabled. Set EWE_MCP_ENABLE_EVAL=1 or enable_eval=1 to opt in.");
            }
            _auditLog.Add("shutdown_server", "executed", "Server shutdown requested");
            var callback = OnShutdownRequested;
            if (callback != null)
            {
                System.Threading.ThreadPool.QueueUserWorkItem(_ =>
                {
                    System.Threading.Thread.Sleep(500);
                    callback();
                });
            }
            return new JObject
            {
                ["success"] = true,
                ["message"] = "Server shutting down in 500ms"
            };
        }

        private JObject ExecuteEweCode(JObject parameters)
        {
            if (!_config.EnableEval)
            {
                _auditLog.Add("execute_ewe_code", "denied", "eval_disabled");
                throw new EwEMcpException("eval_disabled", "execute_ewe_code is disabled. Set EWE_MCP_ENABLE_EVAL=1 to opt in.");
            }

            var code = RequiredString(parameters, "code").Trim();

            var dangerousPattern = IsDangerousExpression(code);
            if (dangerousPattern != null)
            {
                var reason = $"Expression contains disallowed pattern: {dangerousPattern}";
                _auditLog.Add("execute_ewe_code", "denied", reason);
                throw new EwEMcpException("eval_denied", reason);
            }

            _auditLog.Add("execute_ewe_code", "executed", code.Substring(0, Math.Min(code.Length, 200)));
            var core = RequireCore();

            // Handle "Core.Property = Value"
            if (code.StartsWith("Core.", StringComparison.OrdinalIgnoreCase) && code.Contains("="))
            {
                var parts = code.Split(new[] { '=' }, 2);
                var path = parts[0].Trim().Substring(5); // Remove "Core."
                var valStr = parts[1].Trim();

                if (path.Contains("."))
                {
                    var pathParts = path.Split('.');
                    var parent = SafeGetNestedProperty(core, pathParts.Take(pathParts.Length - 1));
                    if (SafeSetProperty(parent, pathParts.Last(), valStr))
                    {
                        return new JObject { ["success"] = true, ["action"] = "set", ["path"] = path, ["value"] = valStr };
                    }
                }
                else
                {
                    if (SafeSetProperty(core, path, valStr))
                    {
                        return new JObject { ["success"] = true, ["action"] = "set", ["property"] = path, ["value"] = valStr };
                    }
                }
            }
            // Handle "Core.Method()" or "Core.Property.Method()"
            else if (code.EndsWith("()"))
            {
                var path = code.Substring(0, code.Length - 2);
                object target = null;
                string methodName = null;

                if (path.StartsWith("Core.", StringComparison.OrdinalIgnoreCase))
                {
                    var pathParts = path.Substring(5).Split('.');
                    target = pathParts.Length > 1 ? SafeGetNestedProperty(core, pathParts.Take(pathParts.Length - 1)) : core;
                    methodName = pathParts.Last();
                }

                if (target != null && methodName != null)
                {
                    var method = target.GetType().GetMethod(methodName, BindingFlags.Instance | BindingFlags.Public, null, Type.EmptyTypes, null);
                    if (method != null)
                    {
                        var res = method.Invoke(target, null);
                        return new JObject { ["success"] = true, ["action"] = "invoke", ["method"] = methodName, ["result"] = ToJToken(res) };
                    }
                }
            }
            // Handle "Core.Property" (with nesting)
            else if (code.StartsWith("Core.", StringComparison.OrdinalIgnoreCase))
            {
                var path = code.Substring(5);
                var val = SafeGetNestedProperty(core, path.Split('.'));
                return new JObject { ["success"] = true, ["action"] = "get", ["path"] = path, ["value"] = ToJToken(val) };
            }

            throw new EwEMcpException("not_implemented", "Complex execute_ewe_code is not implemented. Use 'Core.Prop', 'Core.Prop = Val', or 'Core.Method()'.");
        }

        private static readonly string[] _evalDenyPatterns = new[]
        {
            "System.IO",
            "System.Diagnostics.Process",
            "System.Reflection.Assembly.Load",
            "File.Delete",
            "File.Write",
            "Directory.Delete",
            "System.Net",
            "WebClient",
            "HttpClient",
            "Process.Start",
        };

        private string IsDangerousExpression(string code)
        {
            if (string.IsNullOrEmpty(code)) return null;
            foreach (var pattern in _evalDenyPatterns)
            {
                if (code.IndexOf(pattern, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    return pattern;
                }
            }
            return null;
        }

        private object SafeGetNestedProperty(object obj, IEnumerable<string> path)
        {
            var current = obj;
            foreach (var part in path)
            {
                current = SafeGetProperty(current, part);
                if (current == null) return null;
            }
            return current;
        }

        private bool SafeSetProperty(object obj, string propertyName, object value)
        {
            if (obj == null) return false;
            try
            {
                var property = FindWritableProperty(obj, propertyName, null);
                if (property == null || !property.CanWrite) return false;
                if (!TryConvertValue(value, property.PropertyType, out var convertedValue)) return false;

                property.SetValue(obj, convertedValue, null);
                return true;
            }
            catch
            {
                return false;
            }
        }


        /// <summary>
        /// Mark the EwE data source as modified so that save_model persists
        /// reflection-based property changes to the database.
        /// Mapped domain names to eCoreComponentType enum values (1=Ecopath,
        /// 2=Ecosim, 3=Ecospace, 10=Ecotracer, 15=MSE, ...) so that the
        /// data source's SetChanged() properly tracks which component is dirty.
        private void ForceValidateToDataSource(object entity, string propertyName, object value, string domain)
        {
            var normalizedDomain = (domain ?? "").Trim().ToLowerInvariant();
            var col = SqlColumnFor(propertyName, normalizedDomain);
            if (col == null) return;

            var (tableName, idColumn, idValue) = ResolveDomainSqlInfo(normalizedDomain, entity);
            if (tableName == null || idColumn == null || idValue == null) return;

            var core = _core();
            if (core == null) return;
            var ds = SafeGetProperty(core, "DataSource");
            if (ds == null) return;
            var db = SafeGetProperty(ds, "Database");
            if (db == null) return;
            var connStr = SafeGetProperty(db, "ConnectionString") as string;
            if (string.IsNullOrEmpty(connStr)) return;

            try
            {
                using (var conn = new OleDbConnection(connStr))
                {
                    conn.Open();
                    using (var cmd = new OleDbCommand($"UPDATE [{tableName}] SET [{col}] = @v WHERE [{idColumn}] = @id", conn))
                    {
                        if (col == "GroupName" || col == "FleetName")
                        {
                            cmd.Parameters.AddWithValue("@v", value?.ToString() ?? "");
                        }
                        else
                        {
                            cmd.Parameters.AddWithValue("@v", Convert.ToDouble(value));
                        }
                        cmd.Parameters.AddWithValue("@id", Convert.ToInt32(idValue));
                        cmd.ExecuteNonQuery();
                    }
                }
            }
            catch (Exception ex)
            {
                EwEMcpFileLog.Warn($"SQL persist '{propertyName}' failed: {ex.Message}");
            }

            // Best-effort array sync for save_model compatibility
            try { SyncArrayField(core, entity, normalizedDomain); MarkDataSourceModified(normalizedDomain); } catch { }
        }

        private (string tableName, string idColumn, object idValue) ResolveDomainSqlInfo(string normalizedDomain, object entity)
        {
            switch (normalizedDomain)
            {
                case "ecopath_groups":
                    return ("EcopathGroup", "GroupID",
                        SafeGetProperty(entity, "GroupID") ?? SafeGetProperty(entity, "DBID"));
                case "ecopath_fleets":
                    return ("EcopathFleet", "FleetID",
                        SafeGetProperty(entity, "FleetID") ?? SafeGetProperty(entity, "DBID"));
                case "ecosim_group_inputs":
                    return ("EcosimScenarioGroup", "GroupID",
                        SafeGetProperty(entity, "GroupID") ?? SafeGetProperty(entity, "DBID"));
                case "ecosim_fleet_inputs":
                    return ("EcosimScenarioFleet", "FleetID",
                        SafeGetProperty(entity, "FleetID") ?? SafeGetProperty(entity, "DBID"));
                default:
                    return (null, null, null);
            }
        }

        private void SyncArrayField(object core, object entity, string normalizedDomain)
        {
            // Array field sync is only applicable to Ecopath groups;
            // Ecosim and other domains use different in-memory structures.
            if (normalizedDomain != "ecopath_groups") return;

            var dsData = SafeGetProperty(core, "EcopathDataStructures");
            if (dsData == null) return;
            var idx = SafeGetProperty(entity, "Index");
            if (idx == null) return;
            int i = Convert.ToInt32(idx) + 1;

            CopyProp(dsData, "PBinput", i, entity, "PBInput");
            CopyProp(dsData, "QBinput", i, entity, "QBInput");
            CopyProp(dsData, "EEinput", i, entity, "EEInput");
            CopyProp(dsData, "Binput", i, entity, "BiomassAreaInput", true);
        }

        private static void CopyProp(object dsData, string fieldName, int oneBasedIndex, object entity, string propName, bool multiplyByArea = false)
        {
            var fld = dsData.GetType().GetField(fieldName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (fld == null) return;
            var arr = fld.GetValue(dsData) as Array;
            if (arr == null || oneBasedIndex < 0 || oneBasedIndex >= arr.Length) return;

            var prop = entity.GetType().GetProperty(propName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.FlattenHierarchy);
            if (prop == null) return;
            var val = prop.GetValue(entity);
            if (val == null) return;
            float fv = Convert.ToSingle(val);
            if (multiplyByArea)
            {
                var area = SafeGetProperty(entity, "Area");
                if (area != null) fv *= Convert.ToSingle(area);
            }
            arr.SetValue(fv, oneBasedIndex);
        }

        private static string SqlColumnFor(string propertyName, string normalizedDomain)
        {
            if (normalizedDomain == "ecopath_groups")
            {
                switch (propertyName)
                {
                    case "Name":
                    case "name": return "GroupName";
                    case "BiomassAreaInput": return "Biomass";
                    case "PBInput": return "ProdBiom";
                    case "QBInput": return "ConsBiom";
                    case "EEInput": return "EcoEfficiency";
                    case "OtherMortInput": return "OtherMort";
                    case "BAInput": return "BiomAcc";
                    case "GSInput": return "Unassim";
                    case "DetImport": return "DtImports";
                    case "GEInput": return "ProdCons";
                    case "Immigration": return "Immigration";
                    case "Emigration": return "Emigration";
                    case "EmigRate": return "EmigRate";
                    default: return null;
                }
            }
            if (normalizedDomain == "ecopath_fleets")
            {
                switch (propertyName)
                {
                    case "Name":
                    case "name": return "FleetName";
                    case "NominalEffort": return "NominalEffort";
                    case "FixedCost": return "FixedCost";
                    case "VariableCost": return "VariableCost";
                    default: return null;
                }
            }
            if (normalizedDomain == "ecosim_group_inputs")
            {
                switch (propertyName)
                {
                    case "Pbmaxs": return "Pbmaxs";
                    case "FtimeMax": return "FtimeMax";
                    case "FtimeAdjust": return "FtimeAdjust";
                    default: return null;
                }
            }
            if (normalizedDomain == "ecosim_fleet_inputs")
            {
                switch (propertyName)
                {
                    case "RelQ": return "RelQ";
                    case "EffortConversionFactor": return "EffortConversionFactor";
                    case "EffortAdjustmentFactor": return "EffortAdjustmentFactor";
                    case "SailCost": return "SailCost";
                    default: return null;
                }
            }
            return null;
        }

        private void MarkDataSourceModified(string domain)
        {
            var core = _core();
            if (core == null) return;

            var dataSource = SafeGetProperty(core, "DataSource");
            if (dataSource == null) return;

            int componentValue;
            switch ((domain ?? string.Empty).Trim().ToLowerInvariant())
            {
                case "ecopath_groups":
                case "ecopath_fleets":
                case "ecopath_group_outputs":
                case "ecopath_fleet_outputs":
                case "ecopath_stats":
                    componentValue = 1; // eCoreComponentType.Ecopath
                    break;
                case "ecosim_group_inputs":
                case "ecosim_group_outputs":
                case "ecosim_fleet_inputs":
                case "ecosim_fleet_outputs":
                case "ecosim_scenarios":
                    componentValue = 2; // eCoreComponentType.Ecosim
                    break;
                case "ecospace_group_inputs":
                case "ecospace_group_outputs":
                case "ecospace_fleet_inputs":
                case "ecospace_fleet_outputs":
                case "ecospace_scenarios":
                case "ecospace_habitats":
                case "ecospace_mpas":
                case "ecospace_region_outputs":
                    componentValue = 3; // eCoreComponentType.Ecospace
                    break;
                case "ecotracer_scenarios":
                case "ecotracer_group_inputs":
                case "ecotracer_group_outputs":
                case "ecotracer_model_parameters":
                    componentValue = 10; // eCoreComponentType.Ecotracer
                    break;
                case "mse_parameters":
                case "mse_group_inputs":
                case "mse_group_outputs":
                case "mse_fleet_inputs":
                case "mse_fleet_outputs":
                    componentValue = 15; // eCoreComponentType.MSE
                    break;
                case "monte_carlo_groups":
                    componentValue = 8; // eCoreComponentType.EcoSimMonteCarlo
                    break;
                default:
                    componentValue = 1; // Default to Ecopath
                    break;
            }

            try
            {
                // Resolve the eCoreComponentType enum from the core assembly
                var enumType = dataSource.GetType().Assembly.GetType("EwECore.eCoreComponentType");
                if (enumType == null) return;

                var ccValue = Enum.ToObject(enumType, componentValue);

                // Call DataSource.SetChanged(ccValue) via reflection
                var setChangedMethod = dataSource.GetType().GetMethod("SetChanged",
                    System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public,
                    null, new[] { enumType }, null);
                if (setChangedMethod != null)
                {
                    setChangedMethod.Invoke(dataSource, new object[] { ccValue });

                    // Also notify the StateMonitor so SaveChanges() detects the change
                    var stateMonitor = SafeGetProperty(core, "StateMonitor");
                    if (stateMonitor != null)
                    {
                        var registerMethod = stateMonitor.GetType().GetMethod("RegisterModification",
                            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public,
                            null, new[] { enumType }, null);
                        if (registerMethod != null)
                        {
                            registerMethod.Invoke(stateMonitor, new object[] { ccValue });
                        }
                    }
                }
            }
            catch
            {
                // Best-effort; failing to dirty the data source is not fatal to the set operation itself.
            }
        }

        private cCore RequireCore()
        {
            return _core() ?? throw new EwEMcpException("core_unavailable", "EwE core is not available yet");
        }

        private cCommandHandler RequireCommandHandler()
        {
            return _commandHandler() ?? throw new EwEMcpException("command_handler_unavailable", "EwE command handler is not available yet");
        }

        private static string RequiredString(JObject parameters, string name)
        {
            var value = parameters?[name]?.ToString();
            if (string.IsNullOrWhiteSpace(value))
            {
                throw new EwEMcpException("missing_parameter", $"Missing required parameter: {name}");
            }
            return value;
        }

        private static string OptionalString(JObject parameters, string name)
        {
            var value = parameters?[name]?.ToString();
            return string.IsNullOrWhiteSpace(value) ? null : value;
        }

        private static int RequiredInt(JObject parameters, string name)
        {
            var token = parameters?[name];
            if (token == null || !int.TryParse(token.ToString(), out var value))
            {
                throw new EwEMcpException("missing_parameter", $"Missing required integer parameter: {name}");
            }
            return value;
        }

        private static bool OptionalBool(JObject parameters, string name)
        {
            return OptionalBool(parameters, name, false);
        }

        private static bool OptionalBool(JObject parameters, string name, bool defaultValue)
        {
            var token = parameters?[name];
            if (token == null || token.Type == JTokenType.Null || string.IsNullOrWhiteSpace(token.ToString()))
            {
                return defaultValue;
            }
            if (token.Type == JTokenType.Boolean)
            {
                return token.Value<bool>();
            }
            if (bool.TryParse(token.ToString(), out var value))
            {
                return value;
            }
            throw new EwEMcpException("invalid_parameter", $"Parameter must be a boolean: {name}");
        }

        private static int? OptionalInt(JObject parameters, string name)
        {
            var token = parameters?[name];
            if (token == null || token.Type == JTokenType.Null || string.IsNullOrWhiteSpace(token.ToString()))
            {
                return null;
            }
            if (!int.TryParse(token.ToString(), out var value))
            {
                throw new EwEMcpException("invalid_parameter", $"Parameter must be an integer: {name}");
            }
            return value;
        }

        private static string[] OptionalStringArray(JObject parameters, string name)
        {
            var token = parameters?[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                return null;
            }
            if (token is JArray arr)
            {
                return arr.Select(t => t.ToString()).Where(s => !string.IsNullOrWhiteSpace(s)).ToArray();
            }
            var value = token.ToString();
            return string.IsNullOrWhiteSpace(value) ? null : new[] { value };
        }

        private static int[] RequiredIntArray(JObject parameters, string name)
        {
            var token = parameters?[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                throw new EwEMcpException("missing_parameter", $"Missing required integer array parameter: {name}");
            }

            if (token is JArray arr)
            {
                var values = new List<int>();
                foreach (var item in arr)
                {
                    if (!int.TryParse(item.ToString(), out var value))
                    {
                        throw new EwEMcpException("invalid_parameter", $"All values in {name} must be integers");
                    }
                    values.Add(value);
                }
                if (values.Count == 0)
                {
                    throw new EwEMcpException("invalid_parameter", $"{name} must contain at least one index");
                }
                return values.ToArray();
            }

            if (!int.TryParse(token.ToString(), out var single))
            {
                throw new EwEMcpException("invalid_parameter", $"Parameter must be an integer or integer array: {name}");
            }
            return new[] { single };
        }

        private static JObject GetPluginAssemblyInfo()
        {
            var asm = typeof(EwEMcpPlugin).Assembly;
            return new JObject
            {
                ["assembly"] = asm.GetName().Name,
                ["version"] = asm.GetName().Version?.ToString(),
                ["location"] = asm.Location,
                ["log_path"] = EwEMcpFileLog.Path
            };
        }

        private static string GroupType(object group)
        {
            if (SafeBoolProperty(group, "IsDetritus"))
            {
                return "detritus";
            }
            if (SafeBoolProperty(group, "IsProducer"))
            {
                return "producer";
            }
            if (SafeBoolProperty(group, "IsConsumer"))
            {
                return "consumer";
            }
            return "unknown";
        }

        private static object SafeGetProperty(object obj, string propertyName)
        {
            if (obj == null)
            {
                return null;
            }

            try
            {
                var property = obj.GetType()
                    .GetProperties(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .FirstOrDefault(p => p.Name == propertyName && p.GetIndexParameters().Length == 0);
                if (property != null)
                {
                    return property.GetValue(obj, null);
                }

                var field = obj.GetType()
                    .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    .FirstOrDefault(f => f.Name == propertyName);
                return field?.GetValue(obj);
            }
            catch
            {
                return null;
            }
        }

        private static object SafeGetPropertyWithIndex(object obj, string propertyName, int index)
        {
            if (obj == null) return null;
            try
            {
                var prop = obj.GetType()
                    .GetProperties(BindingFlags.Instance | BindingFlags.Public)
                    .FirstOrDefault(p =>
                    {
                        if (p.Name != propertyName) return false;
                        var parameters = p.GetIndexParameters();
                        return parameters.Length == 1 && parameters[0].ParameterType == typeof(int);
                    });
                if (prop == null) return null;
                return prop.GetValue(obj, new object[] { index });
            }
            catch { return null; }
        }

        private static object SafeGetIndexedProperty(object obj, string propertyName, int index)
        {
            if (obj == null)
            {
                return null;
            }

            try
            {
                var property = obj.GetType()
                    .GetProperties(BindingFlags.Instance | BindingFlags.Public)
                    .FirstOrDefault(p => p.Name == propertyName && p.GetIndexParameters().Length == 1);
                return property?.GetValue(obj, new object[] { index });
            }
            catch
            {
                return null;
            }
        }

        private static object SafeGetIndexedProperty(object obj, string propertyName, int index1, int index2)
        {
            if (obj == null)
            {
                return null;
            }

            try
            {
                var property = obj.GetType()
                    .GetProperties(BindingFlags.Instance | BindingFlags.Public)
                    .FirstOrDefault(p => p.Name == propertyName && p.GetIndexParameters().Length == 2);
                return property?.GetValue(obj, new object[] { index1, index2 });
            }
            catch
            {
                return null;
            }
        }

        private static string SafeStringProperty(object obj, string propertyName)
        {
            return SafeGetProperty(obj, propertyName)?.ToString() ?? string.Empty;
        }

        private static bool SafeBoolProperty(object obj, string propertyName)
        {
            var value = SafeGetProperty(obj, propertyName);
            return value is bool boolValue && boolValue;
        }

        private static int SafeIntProperty(object obj, string propertyName)
        {
            var value = SafeGetProperty(obj, propertyName);
            if (value == null)
            {
                return 0;
            }

            try
            {
                return Convert.ToInt32(value);
            }
            catch
            {
                return 0;
            }
        }

        private static object SafeInvoke(object obj, string methodName)
        {
            if (obj == null)
            {
                return null;
            }

            try
            {
                var method = obj.GetType().GetMethod(methodName, BindingFlags.Instance | BindingFlags.Public, null, Type.EmptyTypes, null);
                return method?.Invoke(obj, null);
            }
            catch
            {
                return null;
            }
        }

        private static bool SafeInvokeBool(object obj, string methodName)
        {
            var value = SafeInvoke(obj, methodName);
            return value is bool boolValue && boolValue;
        }

        private static int SafeInvokeInt(object obj, string methodName)
        {
            var value = SafeInvoke(obj, methodName);
            if (value == null)
            {
                return 0;
            }
            try
            {
                return Convert.ToInt32(value);
            }
            catch
            {
                return 0;
            }
        }

        private static string SafeInvokeToString(object obj, string methodName)
        {
            return SafeInvoke(obj, methodName)?.ToString() ?? string.Empty;
        }

        private static JToken SafeJTokenProperty(object obj, string propertyName)
        {
            return ToJToken(SafeGetProperty(obj, propertyName));
        }

        private static JToken ToJToken(object value)
        {
            try
            {
                return value == null ? JValue.CreateNull() : JToken.FromObject(value);
            }
            catch (Exception ex)
            {
                return new JObject
                {
                    ["error"] = "serialization_failed",
                    ["message"] = ex.Message,
                    ["type"] = value?.GetType().FullName ?? string.Empty
                };
            }
        }

        private static bool TryConvertValue(object value, Type targetType, out object convertedValue)
        {
            convertedValue = null;
            try
            {
                var t = Nullable.GetUnderlyingType(targetType) ?? targetType;
                if (value is JToken token)
                {
                    convertedValue = token.ToObject(t);
                    return true;
                }
                if (t.IsEnum)
                {
                    convertedValue = Enum.Parse(t, value.ToString(), true);
                    return true;
                }
                convertedValue = Convert.ChangeType(value, t);
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static bool IsNonZero(object value)
        {
            if (value == null)
            {
                return false;
            }

            try
            {
                return Math.Abs(Convert.ToDouble(value)) > 0.0000001;
            }
            catch
            {
                return false;
            }
        }

        private JObject ExportActivePlot(JObject parameters)
        {
            var target = OptionalString(parameters, "target") ?? "auto";
            Control captureTarget = ResolveCaptureTarget(target);
            if (captureTarget == null)
            {
                throw new EwEMcpException("capture_unavailable", "No EwE window is available for plot export. Ensure EwE is running with a model loaded.");
            }

            var charts = FindZedGraphControls(captureTarget);
            if (charts.Count == 0)
            {
                return new JObject
                {
                    ["found"] = false,
                    ["suggestion"] = "No ZedGraph chart control found in the active view. Use capture_view for maps and other visuals."
                };
            }

            return ExtractChartData(charts);
        }

        private static List<object> FindZedGraphControls(Control root)
        {
            var charts = new List<object>();
            FindZedGraphControls(root, charts);
            return charts;
        }

        private static void FindZedGraphControls(Control root, List<object> charts)
        {
            if (root == null) return;

            if (root.GetType().Name.Contains("ZedGraphControl"))
            {
                charts.Add(root);
            }

            foreach (Control child in root.Controls)
            {
                FindZedGraphControls(child, charts);
            }
        }

        private JObject ExtractChartData(List<object> charts)
        {
            var panesToInspect = new List<Tuple<int, object>>();
            for (int chartIndex = 0; chartIndex < charts.Count; chartIndex++)
            {
                foreach (var pane in GetZedGraphPanes(charts[chartIndex]))
                {
                    panesToInspect.Add(Tuple.Create(chartIndex, pane));
                }
            }

            if (panesToInspect.Count == 0)
            {
                return new JObject
                {
                    ["found"] = true,
                    ["error"] = "Could not access any GraphPane on the chart control."
                };
            }

            var panes = new JArray();
            var series = new JArray();

            for (int paneIndex = 0; paneIndex < panesToInspect.Count; paneIndex++)
            {
                panes.Add(ExtractPaneData(panesToInspect[paneIndex].Item2, paneIndex, panesToInspect[paneIndex].Item1, series));
            }

            var firstPane = panes.Count > 0 ? panes[0] as JObject : null;
            if (series.Count == 0)
            {
                return new JObject
                {
                    ["found"] = false,
                    ["suggestion"] = "A ZedGraph chart control was found, but no extractable curve series were present. Use capture_view for visual-only charts.",
                    ["chart_count"] = charts.Count,
                    ["pane_count"] = panes.Count,
                    ["panes"] = panes
                };
            }

            return new JObject
            {
                ["found"] = true,
                ["title"] = firstPane?["title"] ?? "",
                ["x_label"] = firstPane?["x_label"] ?? "",
                ["y_label"] = firstPane?["y_label"] ?? "",
                ["chart_count"] = charts.Count,
                ["pane_count"] = panes.Count,
                ["panes"] = panes,
                ["series_count"] = series.Count,
                ["series"] = series
            };
        }

        private static List<object> GetZedGraphPanes(object zgc)
        {
            var panes = new List<object>();
            var seen = new HashSet<object>();

            var masterPane = SafeGetProperty(zgc, "MasterPane");
            var paneList = SafeGetProperty(masterPane, "PaneList");
            int paneCount = SafeCount(paneList);
            for (int i = 0; i < paneCount; i++)
            {
                AddPaneIfNew(panes, seen, SafeGetPropertyWithIndex(paneList, "Item", i));
            }

            AddPaneIfNew(panes, seen, SafeGetProperty(zgc, "GraphPane"));
            return panes;
        }

        private static void AddPaneIfNew(List<object> panes, HashSet<object> seen, object pane)
        {
            if (pane != null && seen.Add(pane))
            {
                panes.Add(pane);
            }
        }

        private static JObject ExtractPaneData(object pane, int paneIndex, int chartIndex, JArray allSeries)
        {
            var titleText = SafeGetProperty(SafeGetProperty(pane, "Title"), "Text")?.ToString() ?? "";
            var xAxis = SafeGetProperty(pane, "XAxis");
            var yAxis = SafeGetProperty(pane, "YAxis");
            var xLabel = SafeGetProperty(SafeGetProperty(xAxis, "Title"), "Text")?.ToString() ?? "";
            var yLabel = SafeGetProperty(SafeGetProperty(yAxis, "Title"), "Text")?.ToString() ?? "";

            var paneSeries = new JArray();
            var curveList = SafeGetProperty(pane, "CurveList");
            int curveCount = SafeCount(curveList);
            for (int i = 0; i < curveCount; i++)
            {
                var curve = SafeGetPropertyWithIndex(curveList, "Item", i);
                if (curve == null) continue;

                var series = ExtractCurveData(curve, paneIndex, chartIndex, titleText, i);
                paneSeries.Add(series);
                allSeries.Add(series);
            }

            return new JObject
            {
                ["index"] = paneIndex,
                ["chart_index"] = chartIndex,
                ["title"] = titleText,
                ["x_label"] = xLabel,
                ["y_label"] = yLabel,
                ["series_count"] = paneSeries.Count,
                ["series"] = paneSeries
            };
        }

        private static JObject ExtractCurveData(object curve, int paneIndex, int chartIndex, string paneTitle, int curveIndex)
        {
            var labelObj = SafeGetProperty(curve, "Label");
            var label = SafeGetProperty(labelObj, "Text")?.ToString();
            if (string.IsNullOrWhiteSpace(label))
            {
                label = !string.IsNullOrWhiteSpace(paneTitle) ? paneTitle : $"Series {curveIndex}";
            }

            var points = SafeGetProperty(curve, "Points");
            var pointCount = SafeCount(points);

            var xVals = new JArray();
            var yVals = new JArray();

            System.Reflection.PropertyInfo xProp = null;
            System.Reflection.PropertyInfo yProp = null;

            for (int j = 0; j < pointCount; j++)
            {
                var point = SafeGetPropertyWithIndex(points, "Item", j);
                if (point == null) continue;

                if (j == 0)
                {
                    var pointType = point.GetType();
                    xProp = pointType.GetProperty("X");
                    yProp = pointType.GetProperty("Y");
                }

                var x = xProp != null ? xProp.GetValue(point) : SafeGetProperty(point, "X");
                var y = yProp != null ? yProp.GetValue(point) : SafeGetProperty(point, "Y");

                if (TryConvertFiniteDouble(x, out var xValue) && TryConvertFiniteDouble(y, out var yValue))
                {
                    xVals.Add(xValue);
                    yVals.Add(yValue);
                }
            }

            return new JObject
            {
                ["name"] = label,
                ["chart_index"] = chartIndex,
                ["pane_index"] = paneIndex,
                ["pane_title"] = paneTitle,
                ["curve_index"] = curveIndex,
                ["x_values"] = xVals,
                ["y_values"] = yVals,
                ["point_count"] = xVals.Count
            };
        }

        private static int SafeCount(object collection)
        {
            if (collection == null) return 0;
            try
            {
                var count = SafeGetProperty(collection, "Count");
                return count != null ? Convert.ToInt32(count) : 0;
            }
            catch
            {
                return 0;
            }
        }

        private static bool TryConvertFiniteDouble(object value, out double result)
        {
            result = 0.0;
            if (value == null) return false;
            try
            {
                result = Convert.ToDouble(value);
                return !double.IsNaN(result) && !double.IsInfinity(result);
            }
            catch
            {
                return false;
            }
        }


        private sealed class EntityTarget
        {
            public EntityTarget(string normalizedDomain, object entity, int? index)
            {
                NormalizedDomain = normalizedDomain;
                Entity = entity;
                Index = index;
            }

            public string NormalizedDomain { get; }
            public object Entity { get; }
            public int? Index { get; }
        }
    }
}
