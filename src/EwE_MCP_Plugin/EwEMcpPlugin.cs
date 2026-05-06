using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
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
            EwEMcpFileLog.Info($"Starting socket server on {config.Host}:{config.Port}");
            try
            {
                var router = new EwEMcpCommandRouter(
                    () => _core,
                    () => _uiContext,
                    () => _commandHandler,
                    () => _syncContext,
                    _auditLog,
                    config);
                _server = new EwEMcpSocketServer(config, router, _auditLog);
                _server.Start();
                EwEMcpFileLog.Info("Socket server started");
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
        private readonly Func<cCore> _core;
        private readonly Func<cUIContext> _uiContext;
        private readonly Func<cCommandHandler> _commandHandler;
        private readonly Func<SynchronizationContext> _syncContext;
        private readonly EwEMcpAuditLog _auditLog;
        private readonly EwEMcpConfig _config;

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
                case "list_ecopath_groups":
                    return ListEcopathGroups();
                case "get_ecopath_group":
                    return GetEcopathGroup(request.Params);
                case "list_ecopath_fleets":
                    return ListEcopathFleets();
                case "get_ecopath_fleet":
                    return GetEcopathFleet(request.Params);
                case "run_ecopath":
                    return OnUiThread(RunEcopath);
                case "run_ecosim":
                    return OnUiThread(RunEcosim);
                case "stop_ecosim":
                    return OnUiThread(StopEcosim);
                case "refresh_ui":
                    return OnUiThread(RefreshUI);
                case "set_values":
                    return OnUiThread(() => SetValues(request.Params));
                case "batch":
                case "batch_commands":
                    return OnUiThread(() => Batch(request.Params));
                case "execute_ewe_code":
                    return OnUiThread(() => ExecuteEweCode(request.Params));
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
            var info = new JObject
            {
                ["loaded"] = SafeGetProperty(core, "DataSource") != null,
                ["data_source"] = SafeGetProperty(core, "DataSource")?.ToString(),
                ["model_name"] = SafeGetProperty(SafeGetProperty(core, "EwEModel"), "Name")?.ToString()
            };
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

            return new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["values"] = ReadScalarValues(target.Entity, variables, true, target.NormalizedDomain)
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
                    result[pair.Key] = new JObject
                    {
                        ["valid"] = false,
                        ["reason"] = "unknown_or_readonly_variable"
                    };
                    continue;
                }

                result[pair.Key] = new JObject
                {
                    ["valid"] = TryConvertValue(pair.Value, property.PropertyType, out _),
                    ["target_property"] = property.Name,
                    ["type"] = FriendlyTypeName(property.PropertyType)
                };
            }

            return new JObject
            {
                ["domain"] = target.NormalizedDomain,
                ["index"] = target.Index.HasValue ? JToken.FromObject(target.Index.Value) : JValue.CreateNull(),
                ["results"] = result
            };
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
                        ["checked"] = cmd.Checked
                    });
                }
            }
            return new JObject { ["commands"] = commands, ["total_count"] = commands.Count };
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

            if (parameters.TryGetValue("tag", out var tag))
            {
                cmd.Tag = tag.Type == JTokenType.Null ? null : ((JValue)tag).Value;
            }
            cmd.Invoke();
            return new JObject { ["invoked"] = true, ["name"] = cmd.Name, ["enabled"] = cmd.Enabled };
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

        private JObject RunEcopath()
        {
            bool ok = RequireCore().RunEcopath();
            return new JObject { ["started"] = ok, ["run_type"] = "ecopath" };
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

        private JObject Batch(JObject parameters)
        {
            var commands = parameters?["commands"] as JArray ?? new JArray();
            var results = new JArray();
            foreach (var item in commands)
            {
                var type = item?["type"]?.ToString();
                if (string.Equals(type, "execute_ewe_code", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(type, "close_model", StringComparison.OrdinalIgnoreCase))
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
            var domain = RequiredString(parameters, "domain").Trim().ToLowerInvariant();
            var index = RequiredInt(parameters, "index");
            var values = parameters["values"] as JObject ?? throw new EwEMcpException("missing_parameter", "Missing 'values' object");
            var core = RequireCore();

            switch (domain)
            {
                case "ecopath_group":
                    return SetEcopathGroupValues(core, index, values);
                case "ecopath_fleet":
                    return SetEcopathFleetValues(core, index, values);
                default:
                    throw new EwEMcpException("unsupported_domain", $"Setting values for domain '{domain}' is not supported yet.");
            }
        }

        private JObject SetEcopathGroupValues(cCore core, int index, JObject values)
        {
            var input = SafeGetIndexedProperty(core, "EcopathGroupInputs", index);
            if (input == null) throw new EwEMcpException("index_out_of_range", $"Group index {index} not found.");

            var results = new JObject();
            foreach (var pair in values)
            {
                var key = pair.Key;
                var value = pair.Value;

                // Map common keys to EwE property names
                string propertyName = null;
                switch (key.ToLowerInvariant())
                {
                    case "name": propertyName = "Name"; break;
                    case "biomass_area": propertyName = "BiomassAreaInput"; break;
                    case "pb": propertyName = "PBInput"; break;
                    case "qb": propertyName = "QBInput"; break;
                    case "ee": propertyName = "EEInput"; break;
                }

                if (propertyName != null)
                {
                    if (SafeSetProperty(input, propertyName, value))
                    {
                        results[key] = "updated";
                    }
                    else
                    {
                        results[key] = "failed_to_set";
                    }
                }
                else
                {
                    results[key] = "unknown_property";
                }
            }

            return new JObject
            {
                ["index"] = index,
                ["results"] = results
            };
        }

        private JObject SetEcopathFleetValues(cCore core, int index, JObject values)
        {
            var fleet = SafeGetIndexedProperty(core, "EcopathFleetInputs", index);
            if (fleet == null) throw new EwEMcpException("index_out_of_range", $"Fleet index {index} not found.");

            var results = new JObject();
            foreach (var pair in values)
            {
                var key = pair.Key;
                var value = pair.Value;

                string propertyName = null;
                switch (key.ToLowerInvariant())
                {
                    case "name": propertyName = "Name"; break;
                }

                if (propertyName != null)
                {
                    if (SafeSetProperty(fleet, propertyName, value))
                    {
                        results[key] = "updated";
                    }
                    else
                    {
                        results[key] = "failed_to_set";
                    }
                }
                else
                {
                    results[key] = "unknown_property";
                }
            }

            return new JObject
            {
                ["index"] = index,
                ["results"] = results
            };
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
                ["writable"] = property.CanWrite && indexParameters.Length == 0,
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
            if (type.IsArray)
            {
                return IsValueLikeType(type.GetElementType());
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

        private JObject ExecuteEweCode(JObject parameters)
        {
            if (!_config.EnableEval)
            {
                throw new EwEMcpException("eval_disabled", "execute_ewe_code is disabled. Set EWE_MCP_ENABLE_EVAL=1 to opt in.");
            }

            var code = RequiredString(parameters, "code").Trim();
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

        private static int RequiredInt(JObject parameters, string name)
        {
            var token = parameters?[name];
            if (token == null || !int.TryParse(token.ToString(), out var value))
            {
                throw new EwEMcpException("missing_parameter", $"Missing required integer parameter: {name}");
            }
            return value;
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
                return property?.GetValue(obj, null);
            }
            catch
            {
                return null;
            }
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
