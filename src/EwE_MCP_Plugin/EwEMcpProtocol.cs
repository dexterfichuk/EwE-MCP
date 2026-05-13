using System;
using System.Collections.Generic;
using System.IO;
using Newtonsoft.Json.Linq;

namespace EwE_MCP_Plugin
{
    internal sealed class EwEMcpConfig
    {
        public const string DefaultHost = "127.0.0.1";
        public const int DefaultPort = 9877;
        public const string DefaultToken = "ewe-mcp-local-dev";

        public string Host { get; private set; }
        public int Port { get; private set; }
        public string Token { get; private set; }
        public bool UsingDefaultToken { get; private set; }
        public bool Enabled { get; private set; }
        public bool EnableEval { get; private set; }
        public bool DialogWatcherEnabled { get; private set; }
        public string DialogBlockedCommands { get; private set; }
        public int MaxMessageBytes { get; private set; }
        public string ConfigFilePath { get; private set; }
        public bool ConfigFileExists { get; private set; }

        public static EwEMcpConfig FromEnvironment()
        {
            var fileValues = ReadConfigFile();
            var configFilePath = GetConfigFilePath();
            var configFileExists = File.Exists(configFilePath);

            var envHost = Environment.GetEnvironmentVariable("EWE_MCP_HOST");
            var envPort = Environment.GetEnvironmentVariable("EWE_MCP_PORT");
            var envToken = Environment.GetEnvironmentVariable("EWE_MCP_TOKEN");
            var envEnabled = Environment.GetEnvironmentVariable("EWE_MCP_ENABLED");
            var envEnableEval = Environment.GetEnvironmentVariable("EWE_MCP_ENABLE_EVAL");
            var envDialogWatcher = Environment.GetEnvironmentVariable("EWE_MCP_DIALOG_WATCHER");
            var envDialogBlocked = Environment.GetEnvironmentVariable("EWE_MCP_DIALOG_BLOCKED_COMMANDS");

            string host;
            if (envHost != null)
            {
                host = envHost;
            }
            else if (fileValues.ContainsKey("host"))
            {
                host = fileValues["host"];
            }
            else
            {
                host = DefaultHost;
            }

            int port;
            if (envPort != null)
            {
                if (!int.TryParse(envPort, out port) || port < 1 || port > 65535)
                    port = DefaultPort;
            }
            else if (fileValues.ContainsKey("port"))
            {
                if (!int.TryParse(fileValues["port"], out port) || port < 1 || port > 65535)
                    port = DefaultPort;
            }
            else
            {
                port = DefaultPort;
            }

            string token;
            bool usingDefaultToken;
            if (!string.IsNullOrWhiteSpace(envToken))
            {
                token = envToken;
                usingDefaultToken = false;
            }
            else if (fileValues.ContainsKey("token") && !string.IsNullOrWhiteSpace(fileValues["token"]))
            {
                token = fileValues["token"];
                usingDefaultToken = false;
            }
            else
            {
                token = DefaultToken;
                usingDefaultToken = true;
            }

            bool enabled;
            if (envEnabled != null)
            {
                enabled = !string.Equals(envEnabled, "0", StringComparison.OrdinalIgnoreCase);
            }
            else if (fileValues.ContainsKey("enabled"))
            {
                enabled = !string.Equals(fileValues["enabled"], "false", StringComparison.OrdinalIgnoreCase)
                          && !string.Equals(fileValues["enabled"], "0", StringComparison.OrdinalIgnoreCase);
            }
            else
            {
                enabled = true;
            }

            bool enableEval;
            if (envEnableEval != null)
            {
                enableEval = string.Equals(envEnableEval, "1", StringComparison.OrdinalIgnoreCase);
            }
            else if (fileValues.ContainsKey("enable_eval"))
            {
                enableEval = string.Equals(fileValues["enable_eval"], "true", StringComparison.OrdinalIgnoreCase)
                             || string.Equals(fileValues["enable_eval"], "1", StringComparison.OrdinalIgnoreCase);
            }
            else
            {
                enableEval = false;
            }

            bool dialogWatcherEnabled;
            if (envDialogWatcher != null)
            {
                dialogWatcherEnabled = string.Equals(envDialogWatcher, "1", StringComparison.OrdinalIgnoreCase);
            }
            else if (fileValues.ContainsKey("dialog_watcher_enabled"))
            {
                dialogWatcherEnabled = string.Equals(fileValues["dialog_watcher_enabled"], "true", StringComparison.OrdinalIgnoreCase)
                    || string.Equals(fileValues["dialog_watcher_enabled"], "1", StringComparison.OrdinalIgnoreCase);
            }
            else
            {
                dialogWatcherEnabled = false;
            }

            string dialogBlockedCommands;
            if (!string.IsNullOrWhiteSpace(envDialogBlocked))
            {
                dialogBlockedCommands = envDialogBlocked;
            }
            else if (fileValues.ContainsKey("dialog_blocked_commands"))
            {
                dialogBlockedCommands = fileValues["dialog_blocked_commands"];
            }
            else
            {
                dialogBlockedCommands = null;
            }

            return new EwEMcpConfig
            {
                Enabled = enabled,
                Host = host,
                Port = port,
                Token = token,
                UsingDefaultToken = usingDefaultToken,
                EnableEval = enableEval,
                DialogWatcherEnabled = dialogWatcherEnabled,
                DialogBlockedCommands = dialogBlockedCommands,
                MaxMessageBytes = 10 * 1024 * 1024,
                ConfigFilePath = configFilePath,
                ConfigFileExists = configFileExists
            };
        }

        private static string GetConfigFilePath()
        {
            var root = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            if (string.IsNullOrWhiteSpace(root))
            {
                root = Path.GetTempPath();
            }
            return Path.Combine(root, "Ecopath with Ecosim", "EwE_MCP_Plugin.cfg");
        }

        private static Dictionary<string, string> ReadConfigFile()
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var path = GetConfigFilePath();
            if (!File.Exists(path))
            {
                return result;
            }

            try
            {
                var inSection = false;
                foreach (var rawLine in File.ReadAllLines(path))
                {
                    var line = rawLine.Trim();
                    if (string.IsNullOrEmpty(line) || line.StartsWith("#", StringComparison.Ordinal) || line.StartsWith(";", StringComparison.Ordinal))
                    {
                        continue;
                    }
                    if (line.StartsWith("[", StringComparison.Ordinal))
                    {
                        inSection = line.Equals("[EwE_MCP]", StringComparison.OrdinalIgnoreCase);
                        continue;
                    }
                    if (!inSection)
                    {
                        continue;
                    }
                    var eq = line.IndexOf('=');
                    if (eq < 1)
                    {
                        continue;
                    }
                    var key = line.Substring(0, eq).Trim();
                    var val = line.Substring(eq + 1).Trim();
                    result[key] = val;
                }
            }
            catch (Exception ex)
            {
                EwEMcpFileLog.Warn($"Could not read config file {path}: {ex.Message}");
            }
            return result;
        }
    }

    internal sealed class EwEMcpRequest
    {
        public string Id { get; set; }
        public string Type { get; set; }
        public JObject Params { get; set; }
        public string Token { get; set; }
        public int? Timeout { get; set; }

        public static EwEMcpRequest FromJson(string json)
        {
            var obj = JObject.Parse(json);
            int? timeout = null;
            if (obj["timeout"] != null && int.TryParse(obj["timeout"].ToString(), out var parsedTimeout))
            {
                timeout = parsedTimeout;
            }

            return new EwEMcpRequest
            {
                Id = obj["id"]?.ToString() ?? Guid.NewGuid().ToString("N"),
                Type = obj["type"]?.ToString(),
                Params = obj["params"] as JObject ?? new JObject(),
                Token = obj["token"]?.ToString(),
                Timeout = timeout
            };
        }
    }

    internal sealed class EwEMcpException : Exception
    {
        public string Code { get; }

        public EwEMcpException(string code, string message)
            : base(message)
        {
            Code = code;
        }
    }

    internal sealed class EwEMcpAuditLog
    {
        private readonly object _gate = new object();
        private readonly Queue<JObject> _entries = new Queue<JObject>();
        private const int MaxEntries = 1000;

        public IEnumerable<JObject> Entries
        {
            get
            {
                lock (_gate)
                {
                    return _entries.ToArray();
                }
            }
        }

        public void Add(string command, string status, string message = null)
        {
            lock (_gate)
            {
                _entries.Enqueue(new JObject
                {
                    ["timestamp_utc"] = DateTime.UtcNow.ToString("o"),
                    ["command"] = command ?? string.Empty,
                    ["status"] = status ?? string.Empty,
                    ["message"] = message ?? string.Empty
                });
                while (_entries.Count > MaxEntries)
                {
                    _entries.Dequeue();
                }
            }
        }
    }
}
