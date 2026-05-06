using System;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;
using EwECore;
using EwECore.Plugins;
using EwECore.Plugins.UI;
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

        public void Initialize(object core)
        {
            lock (_gate)
            {
                _core = core as cCore;
                EnsureServer();
            }
        }

        public void UIContext(object uic)
        {
            lock (_gate)
            {
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
            var router = new EwEMcpCommandRouter(
                () => _core,
                () => _uiContext,
                () => _commandHandler,
                () => _syncContext,
                _auditLog,
                config);
            _server = new EwEMcpSocketServer(config, router, _auditLog);
            _server.Start();
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
                    return Result("pong", true, "plugin", "EwE MCP Bridge");
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
                case "run_ecopath":
                    return OnUiThread(RunEcopath);
                case "run_ecosim":
                    return OnUiThread(RunEcosim);
                case "stop_ecosim":
                    return OnUiThread(StopEcosim);
                case "batch":
                case "batch_commands":
                    return OnUiThread(() => Batch(request.Params));
                case "execute_ewe_code":
                    return ExecuteEweCode();
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
                    Check("token", !string.IsNullOrWhiteSpace(_config.Token), _config.UsingDefaultToken ? "default development token" : "configured"),
                    Check("eval_enabled", _config.EnableEval, _config.EnableEval ? "enabled" : "disabled")
                },
                ["host"] = _config.Host,
                ["port"] = _config.Port
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
                ["app_domain"] = AppDomain.CurrentDomain.FriendlyName
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
            var enumType = core.GetType().Assembly.GetType("EwECore.eCoreCounterTypes");
            if (enumType == null)
            {
                return result;
            }

            foreach (var value in Enum.GetValues(enumType))
            {
                try
                {
                    var method = core.GetType().GetMethod("GetCoreCounter", new[] { enumType });
                    if (method == null)
                    {
                        continue;
                    }
                    var count = method.Invoke(core, new[] { value });
                    result[value.ToString()] = count;
                }
                catch
                {
                    // Some counters may not be valid in every runtime state.
                }
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
                if (string.Equals(type, "execute_ewe_code", StringComparison.OrdinalIgnoreCase))
                {
                    throw new EwEMcpException("blocked_batch_command", "execute_ewe_code is not allowed in batch");
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

        private JObject ExecuteEweCode()
        {
            if (!_config.EnableEval)
            {
                throw new EwEMcpException("eval_disabled", "execute_ewe_code is disabled. Set EWE_MCP_ENABLE_EVAL=1 to opt in.");
            }

            throw new EwEMcpException("not_implemented", "execute_ewe_code is intentionally gated and has not been implemented in this vertical slice.");
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

        private static object SafeGetProperty(object obj, string propertyName)
        {
            if (obj == null)
            {
                return null;
            }
            return obj.GetType().GetProperty(propertyName, BindingFlags.Instance | BindingFlags.Public)?.GetValue(obj, null);
        }
    }
}
