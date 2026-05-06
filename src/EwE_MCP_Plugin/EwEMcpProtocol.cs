using System;
using System.Collections.Generic;
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
        public bool EnableEval { get; private set; }
        public int MaxMessageBytes { get; private set; }

        public static EwEMcpConfig FromEnvironment()
        {
            var token = Environment.GetEnvironmentVariable("EWE_MCP_TOKEN");
            var portRaw = Environment.GetEnvironmentVariable("EWE_MCP_PORT");
            int port;
            if (!int.TryParse(portRaw, out port) || port < 1 || port > 65535)
            {
                port = DefaultPort;
            }

            return new EwEMcpConfig
            {
                Host = Environment.GetEnvironmentVariable("EWE_MCP_HOST") ?? DefaultHost,
                Port = port,
                Token = string.IsNullOrWhiteSpace(token) ? DefaultToken : token,
                UsingDefaultToken = string.IsNullOrWhiteSpace(token),
                EnableEval = string.Equals(Environment.GetEnvironmentVariable("EWE_MCP_ENABLE_EVAL"), "1", StringComparison.OrdinalIgnoreCase),
                MaxMessageBytes = 10 * 1024 * 1024
            };
        }
    }

    internal sealed class EwEMcpRequest
    {
        public string Id { get; set; }
        public string Type { get; set; }
        public JObject Params { get; set; }
        public string Token { get; set; }

        public static EwEMcpRequest FromJson(string json)
        {
            var obj = JObject.Parse(json);
            return new EwEMcpRequest
            {
                Id = obj["id"]?.ToString() ?? Guid.NewGuid().ToString("N"),
                Type = obj["type"]?.ToString(),
                Params = obj["params"] as JObject ?? new JObject(),
                Token = obj["token"]?.ToString()
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
