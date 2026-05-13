using System;
using System.Collections.Generic;
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
                case "list_ecopath_groups":
                    return ListEcopathGroups();
                case "get_ecopath_group":
                    return GetEcopathGroup(request.Params);
                case "list_ecopath_fleets":
                    return ListEcopathFleets();
                case "get_ecopath_fleet":
                    return GetEcopathFleet(request.Params);
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
                case "list_ecosim_fleet_inputs":
                    return ListEcosimFleetInputs();
                case "get_ecosim_fleet_input":
                    return GetEcosimFleetInput(request.Params);
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
                case "export_ecopath_results":
                    return OnUiThread(() => ExportEcopathResults(request.Params));
                case "run_ecosim":
                    return OnUiThread(RunEcosim);
                case "stop_ecosim":
                    return OnUiThread(StopEcosim);
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
                    Check("eval_enabled", _config.EnableEval, _config.EnableEval ? "enabled" : "disabled")
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
                    return ListEcopathGroups();
                case "ecopath_fleets":
                case "ecopath_fleet":
                    return ListEcopathFleets();
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
                variables.Add(PropertyMetadata(property, target.NormalizedDomain));
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
                throw new EwEMcpException("unknown_variable", $"Variable not found: {variable}");
            }
            return PropertyMetadata(property, target.NormalizedDomain);
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
                    result[pair.Key] = new JObject
                    {
                        ["valid"] = false,
                        ["reason"] = "unknown_or_readonly_variable"
                    };
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
            bool ok = RequireCore().LoadEcospaceScenario(index);
            return new JObject { ["loaded"] = ok, ["index"] = index };
        }

        private JObject LoadEcotracerScenario(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
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
                        default: break;
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
                        default: break;
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
            return new JObject
            {
                ["rows"] = SafeIntProperty(basemap, "InRow"),
                ["cols"] = SafeIntProperty(basemap, "InCol"),
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
            bool ok = Convert.ToBoolean(method.Invoke(core, new object[] { name, description, author, contact, rows, cols, lat, lon, cellLength }));
            if (!ok) throw new EwEMcpException("create_failed", "Failed to create Ecospace scenario");
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
                        default: break;
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
            manager.GetType().GetMethod("StopRun")?.Invoke(manager, null);
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
            if (SafeIntProperty(core, "ActiveEcosimScenarioIndex") < 1)
            {
                throw new EwEMcpException("ecosim_scenario_not_loaded", "Load an Ecosim scenario before copying it.");
            }

            var ok = core.SaveEcosimScenarioAs(name, description);
            return new JObject
            {
                ["copied"] = ok,
                ["name"] = name,
                ["active_index"] = SafeIntProperty(core, "ActiveEcosimScenarioIndex")
            };
        }

        private JObject DeleteEcosimScenario(JObject parameters)
        {
            var index = RequiredInt(parameters, "index");
            var core = RequireCore();
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
            var state = new JObject
            {
                ["core_execution_state"] = SafeInvokeToString(sm, "CoreExecutionState"),
                ["is_busy"] = SafeInvokeBool(sm, "IsBusy"),
                ["has_ecopath_loaded"] = SafeInvokeBool(sm, "HasEcopathLoaded"),
                ["has_ecopath_ran"] = SafeInvokeBool(sm, "HasEcopathRan"),
                ["has_ecosim_loaded"] = SafeInvokeBool(sm, "HasEcosimLoaded"),
                ["has_ecosim_initialized"] = SafeInvokeBool(sm, "HasEcosimInitialized"),
                ["is_ecosim_running"] = SafeInvokeBool(sm, "IsEcosimRunning"),
                ["has_ecosim_ran"] = SafeInvokeBool(sm, "HasEcosimRan"),
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

        private JObject GetEcosimGroupResults(JObject parameters)
        {
            var core = RequireCore();
            EnsureEcosimRan(core);
            var index = RequiredInt(parameters, "index");
            var groupCount = SafeIntProperty(core, "nGroups");
            if (index < 1 || index > groupCount)
            {
                throw new EwEMcpException("index_out_of_range", $"Ecosim group result index {index} is outside 1..{groupCount}");
            }
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

        private static readonly HashSet<string> DialogOpeningCommands = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "ImportTimeSeries",
            "m_cmdImportTimeSeries",
        };

        private JObject InvokeUiCommand(JObject parameters)
        {
            var name = RequiredString(parameters, "name");
            var cmdh = RequireCommandHandler();
            var cmd = cmdh.GetCommand(name);
            if (cmd == null)
            {
                throw new EwEMcpException("command_not_found", $"UI command not found: {name}");
            }
            if (DialogOpeningCommands.Contains(name))
            {
                throw new EwEMcpException("dialog_command_blocked", $"UI command '{name}' opens a modal dialog that cannot be handled in MCP mode. Use the dedicated MCP tool instead.");
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

        private JObject LoadModel(JObject parameters)
        {
            var path = RequiredString(parameters, "path");
            var ok = RequireCore().LoadModel(path);
            return new JObject { ["loaded"] = ok, ["path"] = path };
        }

        private JObject SaveModel(JObject parameters, bool saveAs)
        {
            var core = RequireCore();
            string path = parameters?["path"]?.ToString();
            bool ok = saveAs || !string.IsNullOrWhiteSpace(path) ? core.Save(path ?? string.Empty) : core.Save();
            return new JObject { ["saved"] = ok, ["path"] = path ?? string.Empty };
        }

        private JObject CloseModel()
        {
            bool ok = RequireCore().CloseModel();
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
        string runState = isBalanced ? "balanced" : "unbalanced";
        return new JObject
        {
            ["started"] = ok,
            ["run_type"] = "ecopath",
            ["is_model_balanced"] = isBalanced,
            ["ecopath_run_state"] = runState
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
            ["queried_at"] = DateTime.UtcNow.ToString("o")
        };
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
            var fullPath = System.IO.Path.GetFullPath(path);
            if (fullPath.Contains(".."))
            {
                throw new EwEMcpException("invalid_parameter", "path must not contain directory traversal");
            }
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

        private static readonly HashSet<string> BatchBlockedCommands = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "execute_ewe_code",
            "close_model",
            "load_model",
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
        "export_ecopath_results",
        "shutdown_server",
        "invoke_ui_command",
    };

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
            var navigate = OptionalBool(parameters, "navigate");

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
            var navigation = OptionalBool(parameters, "navigate") ? NavigateToSuggestedEditNode(target.NormalizedDomain, new[] { variable }) : null;
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
            }
            catch (Exception ex)
            {
                throw new EwEMcpException("set_failed", ex.Message);
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
                    results[pair.Key] = "unknown_or_readonly_property";
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

            try
            {
                var metadata = SafeGetProperty(entity, "Metadata");
                if (metadata == null) return null;

                var minObj = SafeGetProperty(metadata, "Min");
                var maxObj = SafeGetProperty(metadata, "Max");
                if (minObj == null && maxObj == null) return null;

                double min = minObj != null ? Convert.ToDouble(minObj) : double.MinValue;
                double max = maxObj != null ? Convert.ToDouble(maxObj) : double.MaxValue;

                if (numericValue < min || numericValue > max)
                {
                    return $"Value {numericValue} is outside valid range [{min}, {max}]";
                }
            }
            catch { }

            try
            {
                var varNameProp = SafeGetProperty(entity, "VariableName");
                if (varNameProp != null && varNameProp is string varName && !string.IsNullOrEmpty(varName))
                {
                    var metadataProp = entity.GetType().GetProperty("VariableMetadata(" + varName + ")");
                    if (metadataProp == null) return null;
                    var metadata = metadataProp.GetValue(entity);
                    if (metadata == null) return null;
                    var minObj = SafeGetProperty(metadata, "Min");
                    var maxObj = SafeGetProperty(metadata, "Max");
                    if (minObj == null && maxObj == null) return null;
                    double min = minObj != null ? Convert.ToDouble(minObj) : double.MinValue;
                    double max = maxObj != null ? Convert.ToDouble(maxObj) : double.MaxValue;
                    if (numericValue < min || numericValue > max)
                    {
                        return $"Value {numericValue} is outside valid range [{min}, {max}] for {varName}";
                    }
                }
            }
            catch { }

            return null;
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
                            result[variable] = new JObject { ["error"] = "unknown_variable" };
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
                return ToJToken(property.GetValue(entity, null));
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

        private static JObject PropertyMetadata(PropertyInfo property, string domain)
        {
            var indexParameters = property.GetIndexParameters();
            return new JObject
            {
                ["name"] = ToVariableName(property.Name),
                ["property"] = property.Name,
                ["domain"] = domain ?? string.Empty,
                ["type"] = FriendlyTypeName(property.PropertyType),
                ["readable"] = property.CanRead,
                ["writable"] = property.CanWrite,
                ["indexed"] = indexParameters.Length > 0,
                ["index_count"] = indexParameters.Length
            };
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
            var alias = AliasPropertyName(variable, domain);
            return GetReadableProperties(entity)
                .Where(p => p.CanWrite && p.GetIndexParameters().Length == 0)
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

            var alias = AliasPropertyName(variable, domain);
            return GetReadableProperties(entity)
                .Where(p => p.GetIndexParameters().Length == indexCount)
                .Where(p => !requireWritable || p.CanWrite)
                .FirstOrDefault(p =>
                    string.Equals(p.Name, alias, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(NormalizeVariableName(p.Name), NormalizeVariableName(alias), StringComparison.OrdinalIgnoreCase));
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
            var token = parameters?[name];
            if (token == null || token.Type == JTokenType.Null || string.IsNullOrWhiteSpace(token.ToString()))
            {
                return false;
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
                    .GetProperties(BindingFlags.Instance | BindingFlags.Public)
                    .FirstOrDefault(p => p.Name == propertyName && p.GetIndexParameters().Length == 0);
                if (property != null)
                {
                    return property.GetValue(obj, null);
                }

                var field = obj.GetType()
                    .GetFields(BindingFlags.Instance | BindingFlags.Public)
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
