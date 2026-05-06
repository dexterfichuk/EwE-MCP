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
                    Domain("ecopath_fleets", "Ecopath fleets with landings/discards by group", true, true),
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
                    return ListEcopathGroups();
                case "ecopath_fleets":
                    return ListEcopathFleets();
                case "ui_commands":
                    return ListUiCommands();
                default:
                    throw new EwEMcpException("unknown_domain", $"Unknown domain: {domain}");
            }
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

        private JObject ExecuteEweCode(JObject parameters)
        {
            if (!_config.EnableEval)
            {
                throw new EwEMcpException("eval_disabled", "execute_ewe_code is disabled. Set EWE_MCP_ENABLE_EVAL=1 to opt in.");
            }

            var code = RequiredString(parameters, "code");
            var core = RequireCore();
            if (code.StartsWith("Core.", StringComparison.OrdinalIgnoreCase))
            {
                var member = code.Substring(5).Trim();
                if (member.Contains("="))
                {
                    var parts = member.Split(new[] { '=' }, 2);
                    var propName = parts[0].Trim();
                    var valStr = parts[1].Trim();
                    if (SafeSetProperty(core, propName, valStr))
                    {
                        return new JObject { ["success"] = true, ["action"] = "set", ["property"] = propName, ["value"] = valStr };
                    }
                }
                else
                {
                    var val = SafeGetProperty(core, member);
                    return new JObject { ["success"] = true, ["action"] = "get", ["property"] = member, ["value"] = ToJToken(val) };
                }
            }

            throw new EwEMcpException("not_implemented", "Complex execute_ewe_code is not implemented. Use 'Core.Property' or 'Core.Property = Value'.");
        }

        private bool SafeSetProperty(object obj, string propertyName, object value)
        {
            if (obj == null) return false;
            try
            {
                var property = obj.GetType().GetProperty(propertyName, BindingFlags.Instance | BindingFlags.Public);
                if (property == null || !property.CanWrite) return false;

                object convertedValue;
                if (value is JToken token)
                {
                    convertedValue = token.ToObject(property.PropertyType);
                }
                else
                {
                    convertedValue = Convert.ChangeType(value, property.PropertyType);
                }

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
            return value == null ? JValue.CreateNull() : JToken.FromObject(value);
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
    }
}
