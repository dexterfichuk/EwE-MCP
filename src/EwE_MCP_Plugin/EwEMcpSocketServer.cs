using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace EwE_MCP_Plugin
{
    internal sealed class EwEMcpSocketServer
    {
        private readonly EwEMcpConfig _config;
        private readonly EwEMcpCommandRouter _router;
        private readonly EwEMcpAuditLog _auditLog;
        private TcpListener _listener;
        private CancellationTokenSource _cts;
        private Task _acceptLoop;
        private CancellationTokenSource _dialogWatcherCts;
        private Task _dialogWatcherLoop;
        // Concurrent queue of captured dialog events for piggyback delivery
        private readonly ConcurrentQueue<JObject> _pendingDialogEvents = new ConcurrentQueue<JObject>();

        private int _currentPid;

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern IntPtr GetWindow(IntPtr hWnd, uint uCmd);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

        [DllImport("user32.dll")]
        private static extern IntPtr FindWindowEx(IntPtr hWndParent, IntPtr hWndChildAfter, string lpszClass, string lpszWindow);

        [DllImport("user32.dll")]
        private static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool SendMessageTimeout(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam, uint fuFlags, uint uTimeout, out IntPtr lpdwResult);

        [DllImport("user32.dll")]
        private static extern int GetDlgCtrlID(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern bool EnumChildWindows(IntPtr hWnd, EnumWindowsProc lpEnumFunc, IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowTextLength(IntPtr hWnd);

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        private const uint GW_OWNER = 4;
        private const uint BM_CLICK = 0x00F5;
        private const uint WM_KEYDOWN = 0x0100;
        private const uint WM_COMMAND = 0x0111;
        private const int VK_RETURN = 0x0D;
        private const int IDOK = 1;
        private const int IDYES = 6;
        private const uint SMTO_ABORTIFHUNG = 0x0002;
        private const uint MSG_TIMEOUT_MS = 500;

        public EwEMcpSocketServer(EwEMcpConfig config, EwEMcpCommandRouter router, EwEMcpAuditLog auditLog)
        {
            _config = config;
            _router = router;
            _auditLog = auditLog;
        }

        public void Start()
        {
            if (_listener != null)
            {
                return;
            }

            _cts = new CancellationTokenSource();
            IPAddress address;
            if (!IPAddress.TryParse(_config.Host, out address))
            {
                address = IPAddress.Loopback;
            }
            _listener = new TcpListener(address, _config.Port);
            _listener.Start();
            _acceptLoop = Task.Run(() => AcceptLoopAsync(_cts.Token));
            StartDialogWatcher();
            _auditLog.Add("server", "started", $"{_config.Host}:{_config.Port}");
        }

        public void Stop()
        {
            try
            {
                _cts?.Cancel();
                _listener?.Stop();
                _acceptLoop?.Wait(1000);
                StopDialogWatcher();
            }
            catch
            {
                // Shutdown must never prevent EwE from closing.
            }
            finally
            {
                _listener = null;
                _cts = null;
                _acceptLoop = null;
                _auditLog.Add("server", "stopped");
            }
        }

        private async Task AcceptLoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                TcpClient client = null;
                try
                {
                    client = await _listener.AcceptTcpClientAsync().ConfigureAwait(false);
                    client.NoDelay = true;
                    _ = Task.Run(() => HandleClientAsync(client, token), token);
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (Exception ex)
                {
                    client?.Close();
                    _auditLog.Add("accept", "error", ex.Message);
                    await Task.Delay(250, token).ConfigureAwait(false);
                }
            }
        }

        private async Task HandleClientAsync(TcpClient client, CancellationToken token)
        {
            using (client)
            using (var stream = client.GetStream())
            {
                while (!token.IsCancellationRequested && client.Connected)
                {
                    try
                    {
                        var requestJson = await ReadFrameAsync(stream, token).ConfigureAwait(false);
                        if (requestJson == null)
                        {
                            return;
                        }

                        var response = await ExecuteAsync(requestJson, token).ConfigureAwait(false);
                        byte[] responsePayload;
                        long responseBytes;
                        if (!TrySerializeResponse(response, _config.MaxMessageBytes, out responsePayload, out responseBytes))
                        {
                            var commandType = ParseCommandType(requestJson);
                            _auditLog.Add(commandType, "response_oversized", $"Response {responseBytes} bytes exceeds limit {_config.MaxMessageBytes}");
                            var oversizedResponse = new JObject
                            {
                                ["id"] = response["id"] ?? string.Empty,
                                ["status"] = "error",
                                ["error"] = new JObject
                                {
                                    ["code"] = "response_too_large",
                                    ["message"] = $"Response payload ({responseBytes} bytes) exceeds the size limit ({_config.MaxMessageBytes} bytes). Try requesting smaller subsets of data."
                                }
                            };
                            responsePayload = SerializeResponse(oversizedResponse);
                        }
                        await WriteFrameAsync(stream, responsePayload, token).ConfigureAwait(false);
                    }
                    catch (IOException)
                    {
                        return;
                    }
                    catch (Exception ex)
                    {
                        _auditLog.Add("client", "error", ex.Message);
                        return;
                    }
                }
            }
        }

        private async Task<JObject> ExecuteAsync(string requestJson, CancellationToken baseToken)
        {
            EwEMcpRequest request = null;
            try
            {
                request = EwEMcpRequest.FromJson(requestJson);
                if (string.IsNullOrWhiteSpace(request.Type))
                {
                    throw new EwEMcpException("missing_type", "Missing command type");
                }
                if (!string.Equals(request.Token, _config.Token, StringComparison.Ordinal))
                {
                    throw new EwEMcpException("unauthorized", "Invalid or missing EWE_MCP_TOKEN");
                }

                var timeoutMs = request.Timeout.HasValue ? request.Timeout.Value * 1000 : Timeout.Infinite;
                using (var cts = CancellationTokenSource.CreateLinkedTokenSource(baseToken))
                {
                    if (timeoutMs > 0)
                    {
                        cts.CancelAfter(timeoutMs);
                    }

                    var tcs = new TaskCompletionSource<JObject>();

                    _ = Task.Run(() =>
                    {
                        try
                        {
                var result = _router.Dispatch(request);
                        _auditLog.Add(request.Type, "success", request.Params?.ToString(Formatting.None));

                        var responseObj = new JObject
                        {
                            ["id"] = request.Id,
                            ["status"] = "success",
                            ["result"] = result ?? new JObject()
                        };

                        // Piggyback pending dialog events onto the response
                        if (!_pendingDialogEvents.IsEmpty)
                        {
                            var events = new JArray();
                            while (_pendingDialogEvents.TryDequeue(out var evt))
                            {
                                events.Add(evt);
                            }
                            responseObj["_events"] = events;
                        }

                        tcs.TrySetResult(responseObj);
                        }
                        catch (Exception ex)
                        {
                            tcs.TrySetException(ex);
                        }
                    }, cts.Token);

                    using (cts.Token.Register(() => tcs.TrySetCanceled()))
                    {
                        try
                        {
                            return await tcs.Task.ConfigureAwait(false);
                        }
                        catch (OperationCanceledException)
                        {
                            throw new EwEMcpException("timeout", $"Command {request.Type} timed out after {request.Timeout} seconds");
                        }
                    }
                }
            }
            catch (EwEMcpException ex)
            {
                _auditLog.Add(request?.Type, "error", ex.Message);
                return ErrorResponse(request?.Id, ex.Code, ex.Message);
            }
            catch (Exception ex)
            {
                _auditLog.Add(request?.Type, "error", ex.Message);
                return ErrorResponse(request?.Id, "exception", ex.Message);
            }
        }

        private static JObject ErrorResponse(string id, string code, string message)
        {
            return new JObject
            {
                ["id"] = id ?? string.Empty,
                ["status"] = "error",
                ["error"] = new JObject
                {
                    ["code"] = code,
                    ["message"] = message
                }
            };
        }

        private async Task<string> ReadFrameAsync(NetworkStream stream, CancellationToken token)
        {
            var header = await ReadExactAsync(stream, 4, token).ConfigureAwait(false);
            if (header == null)
            {
                return null;
            }

            var length = (uint)IPAddress.NetworkToHostOrder(BitConverter.ToInt32(header, 0));
            if (length > _config.MaxMessageBytes)
            {
                throw new InvalidDataException($"Message too large: {length} bytes");
            }

            var payload = await ReadExactAsync(stream, (int)length, token).ConfigureAwait(false);
            return payload == null ? null : Encoding.UTF8.GetString(payload);
        }

        private static async Task<byte[]> ReadExactAsync(NetworkStream stream, int length, CancellationToken token)
        {
            var buffer = new byte[length];
            var offset = 0;
            while (offset < length)
            {
                var read = await stream.ReadAsync(buffer, offset, length - offset, token).ConfigureAwait(false);
                if (read == 0)
                {
                    return null;
                }
                offset += read;
            }
            return buffer;
        }

        private static string ParseCommandType(string requestJson)
        {
            try
            {
                return JObject.Parse(requestJson)?["type"]?.ToString() ?? "unknown";
            }
            catch
            {
                return "unknown";
            }
        }

        private static bool TrySerializeResponse(JObject response, int maxBytes, out byte[] payload, out long bytesAttempted)
        {
            var stream = new SizeLimitedMemoryStream(maxBytes);
            var streamWriter = new StreamWriter(stream, new UTF8Encoding(false));
            var jsonWriter = new JsonTextWriter(streamWriter) { Formatting = Formatting.None };

            try
            {
                response.WriteTo(jsonWriter);
                jsonWriter.Flush();
                streamWriter.Flush();
                payload = stream.ToArray();
                bytesAttempted = payload.Length;
                return true;
            }
            catch (ResponseTooLargeException ex)
            {
                payload = null;
                bytesAttempted = ex.BytesAttempted;
                return false;
            }
            finally
            {
                try
                {
                    jsonWriter.Close();
                }
                catch (Exception)
                {
                    // The overflow is already captured above; closing may flush buffered bytes.
                }
            }
        }

        private static byte[] SerializeResponse(JObject response)
        {
            return Encoding.UTF8.GetBytes(response.ToString(Formatting.None));
        }

        private static async Task WriteFrameAsync(NetworkStream stream, byte[] payload, CancellationToken token)
        {
            var header = new byte[4];
            var networkLength = IPAddress.HostToNetworkOrder(payload.Length);
            var lengthBytes = BitConverter.GetBytes(networkLength);
            Buffer.BlockCopy(lengthBytes, 0, header, 0, header.Length);
            await stream.WriteAsync(header, 0, header.Length, token).ConfigureAwait(false);
            await stream.WriteAsync(payload, 0, payload.Length, token).ConfigureAwait(false);
            await stream.FlushAsync(token).ConfigureAwait(false);
        }

        private void StartDialogWatcher()
        {
            if (!_config.DialogWatcherEnabled)
            {
                EwEMcpFileLog.Info("Dialog watcher is disabled (dialog_watcher_enabled not set)");
                return;
            }
            _currentPid = System.Diagnostics.Process.GetCurrentProcess().Id;
            _dialogWatcherCts = new CancellationTokenSource();
            _dialogWatcherLoop = Task.Run(() => DialogWatcherLoopAsync(_dialogWatcherCts.Token));
            EwEMcpFileLog.Info($"Dialog watcher started (PID={_currentPid})");
        }

        private void StopDialogWatcher()
        {
            try
            {
                _dialogWatcherCts?.Cancel();
                _dialogWatcherLoop?.Wait(2000);
            }
            catch (Exception ex)
            {
                EwEMcpFileLog.Warn($"Dialog watcher stop error: {ex.Message}");
            }
            finally
            {
                _dialogWatcherCts?.Dispose();
                _dialogWatcherCts = null;
                _dialogWatcherLoop = null;
            }
        }

        private async Task DialogWatcherLoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(2000, token).ConfigureAwait(false);
                    var dismissed = CaptureAndDismissDialogs(_currentPid);
                    if (dismissed > 0)
                    {
                        _auditLog.Add("dialog_watcher", "dismissed", $"{dismissed} blocking dialog(s) auto-dismissed");
                    }
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    EwEMcpFileLog.Warn($"Dialog watcher error: {ex.Message}");
                }
            }
        }

        /// <summary>Capture the body text from a dialog's STATIC child controls.</summary>
        private static string CaptureDialogText(IntPtr hWnd)
        {
            var textBuilder = new StringBuilder(4096);
            EnumChildWindows(hWnd, (child, _) =>
            {
                var className = new StringBuilder(256);
                GetClassName(child, className, className.Capacity);
                if (string.Equals(className.ToString(), "Static", StringComparison.OrdinalIgnoreCase))
                {
                    var len = GetWindowTextLength(child);
                    if (len > 0)
                    {
                        var buf = new StringBuilder(len + 1);
                        GetWindowText(child, buf, buf.Capacity);
                        var t = buf.ToString().Trim();
                        if (!string.IsNullOrEmpty(t))
                        {
                            if (textBuilder.Length > 0)
                                textBuilder.Append(" | ");
                            textBuilder.Append(t);
                        }
                    }
                }
                return true;
            }, IntPtr.Zero);
            return textBuilder.ToString().Trim();
        }

        // BM_CLICK via SendMessageTimeout marshals to the dialog's UI thread. The timeout
        // and PostMessage fallback keep the watcher from hanging if a dialog stops pumping.
        private int CaptureAndDismissDialogs(int currentPid)
        {
            var dismissed = 0;
            var events = new List<JObject>();

            EnumWindows((hWnd, _) =>
            {
                GetWindowThreadProcessId(hWnd, out var pid);
                if (pid != currentPid) return true;
                if (!IsWindowVisible(hWnd)) return true;

                var owner = GetWindow(hWnd, GW_OWNER);
                if (owner == IntPtr.Zero) return true;

                var className = new StringBuilder(256);
                GetClassName(hWnd, className, className.Capacity);
                var cls = className.ToString();

                var title = new StringBuilder(512);
                GetWindowText(hWnd, title, title.Capacity);
                var titleText = title.ToString();
                var displayTitle = string.IsNullOrEmpty(titleText) ? "(untitled)" : titleText;

                var bodyText = CaptureDialogText(hWnd);
                var safeToDismiss = !IsRiskyDialog(displayTitle, bodyText);

                // Check if there's an OK/Yes button we could click
                bool hasOkButton = false;
                if (cls == "#32770")
                {
                    var btn = FindWindowEx(hWnd, IntPtr.Zero, "Button", null);
                    while (btn != IntPtr.Zero)
                    {
                        var btnId = GetDlgCtrlID(btn);
                        if (btnId == IDYES || btnId == IDOK)
                        {
                            hasOkButton = true;
                            break;
                        }
                        btn = FindWindowEx(hWnd, btn, "Button", null);
                    }
                }
                else
                {
                    hasOkButton = TryFindYesOrOkButton(hWnd, new HashSet<IntPtr>());
                }

                // Build the dialog event
                var evt = new JObject
                {
                    ["event"] = "dialog_detected",
                    ["data"] = new JObject
                    {
                        ["title"] = displayTitle,
                        ["text"] = bodyText,
                        ["safe_to_dismiss"] = safeToDismiss && hasOkButton,
                        ["has_ok_button"] = hasOkButton,
                        ["timestamp_utc"] = DateTime.UtcNow.ToString("o")
                    }
                };
                events.Add(evt);

                // Only auto-dismiss if safe
                if (safeToDismiss && hasOkButton)
                {
                    if (cls == "#32770")
                    {
                        var btn = FindWindowEx(hWnd, IntPtr.Zero, "Button", null);
                        while (btn != IntPtr.Zero)
                        {
                            var btnId = GetDlgCtrlID(btn);
                            if (btnId == IDYES || btnId == IDOK)
                            {
                                ClickDialogButton(btn, $"Win32 dialog '{displayTitle}' (class #32770, button ID {btnId})");
                                dismissed++;
                                break;
                            }
                            btn = FindWindowEx(hWnd, btn, "Button", null);
                        }
                    }
                    else
                    {
                        var clicked = TryFindAndClickButton(hWnd, new HashSet<IntPtr>(), out var foundText);
                        if (clicked)
                        {
                            EwEMcpFileLog.Info($"Dialog watcher clicked '{foundText}' on WinForms modal '{displayTitle}' (class {cls})");
                            dismissed++;
                        }
                        else if (IsSafeToDismiss(displayTitle))
                        {
                            PostMessage(hWnd, WM_KEYDOWN, (IntPtr)VK_RETURN, IntPtr.Zero);
                            Task.Delay(100).Wait();
                            PostMessage(hWnd, WM_COMMAND, (IntPtr)IDYES, IntPtr.Zero);
                            Task.Delay(100).Wait();
                            PostMessage(hWnd, WM_COMMAND, (IntPtr)IDOK, IntPtr.Zero);
                            EwEMcpFileLog.Info($"Dialog watcher sent Enter/Yes/OK to known-safe dialog '{displayTitle}'");
                            dismissed++;
                        }
                    }
                }
                else
                {
                    EwEMcpFileLog.Warn($"Dialog watcher found dialog '{displayTitle}' — NOT auto-dismissing (safe={safeToDismiss}, hasOk={hasOkButton}). Text: {bodyText}");
                }

                return true;
            }, IntPtr.Zero);

            // Queue all captured events
            foreach (var evt in events)
            {
                _pendingDialogEvents.Enqueue(evt);
                _auditLog.Add("dialog_watcher", "captured", evt["data"]?.ToString(Formatting.None) ?? "unknown");
            }

            return dismissed;
        }

        private static readonly string[] _riskyTextPatterns = new[]
        {
            "delete", "remove", "overwrite", "replace", "discard",
            "are you sure", "confirm", "warning", "unsaved",
            "save changes", "do you want to save"
        };

        private static bool IsRiskyDialog(string title, string text)
        {
            var combined = (title + " " + text).ToLowerInvariant();
            foreach (var pattern in _riskyTextPatterns)
            {
                if (combined.Contains(pattern))
                    return true;
            }
            return false;
        }

        private static bool TryFindYesOrOkButton(IntPtr parent, HashSet<IntPtr> visited)
        {
            var child = FindWindowEx(parent, IntPtr.Zero, null, null);
            while (child != IntPtr.Zero)
            {
                if (!visited.Add(child))
                {
                    child = FindWindowEx(parent, child, null, null);
                    continue;
                }
                var bt = new StringBuilder(256);
                GetWindowText(child, bt, bt.Capacity);
                var text = bt.ToString().Trim();
                if (string.Equals(text, "Yes", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(text, "&Yes", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(text, "OK", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(text, "&OK", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
                if (TryFindYesOrOkButton(child, visited))
                    return true;
                child = FindWindowEx(parent, child, null, null);
            }
            return false;
        }

        private static readonly HashSet<string> _safeDialogTitles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "Ecopath with Ecosim",
            "EwE",
            "Ecopath",
        };

        private static bool IsSafeToDismiss(string title)
        {
            foreach (var safe in _safeDialogTitles)
            {
                if (title.IndexOf(safe, StringComparison.OrdinalIgnoreCase) >= 0)
                    return true;
            }
            return false;
        }

        /// <summary>Recursively search all descendants for a clickable button
        /// with Yes/OK text. Clicks the first match and returns true.</summary>
        private static bool TryFindAndClickButton(IntPtr parent, HashSet<IntPtr> visited, out string buttonText)
        {
            buttonText = null;
            var child = FindWindowEx(parent, IntPtr.Zero, null, null);
            while (child != IntPtr.Zero)
            {
                if (!visited.Add(child))
                {
                    child = FindWindowEx(parent, child, null, null);
                    continue;
                }
                var bt = new StringBuilder(256);
                GetWindowText(child, bt, bt.Capacity);
                var text = bt.ToString().Trim();
                if (string.Equals(text, "Yes", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(text, "&Yes", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(text, "OK", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(text, "&OK", StringComparison.OrdinalIgnoreCase))
                {
                    buttonText = text;
                    ClickDialogButton(child, $"WinForms button '{text}'");
                    return true;
                }
                if (TryFindAndClickButton(child, visited, out var deeperText))
                {
                    buttonText = deeperText;
                    return true;
                }
                child = FindWindowEx(parent, child, null, null);
            }
            return false;
        }

        private static void ClickDialogButton(IntPtr buttonHandle, string description)
        {
            IntPtr result;
            if (!SendMessageTimeout(buttonHandle, BM_CLICK, IntPtr.Zero, IntPtr.Zero, SMTO_ABORTIFHUNG, MSG_TIMEOUT_MS, out result))
            {
                PostMessage(buttonHandle, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
                EwEMcpFileLog.Info($"Dialog watcher: SendMessageTimeout timed out for {description}; fell back to PostMessage");
            }
            else
            {
                EwEMcpFileLog.Info($"Dialog watcher dismissed {description}");
            }
        }

        private sealed class ResponseTooLargeException : Exception
        {
            public ResponseTooLargeException(long bytesAttempted, int limit)
                : base($"Response exceeded {limit} bytes")
            {
                BytesAttempted = bytesAttempted;
            }

            public long BytesAttempted { get; }
        }

        private sealed class SizeLimitedMemoryStream : Stream
        {
            private readonly MemoryStream _inner = new MemoryStream();
            private readonly int _limit;
            private long _bytesAttempted;

            public SizeLimitedMemoryStream(int limit)
            {
                _limit = limit;
            }

            public override bool CanRead => false;
            public override bool CanSeek => false;
            public override bool CanWrite => true;
            public override long Length => _inner.Length;

            public override long Position
            {
                get => _inner.Position;
                set => throw new NotSupportedException();
            }

            public override void Flush()
            {
                _inner.Flush();
            }

            public override int Read(byte[] buffer, int offset, int count)
            {
                throw new NotSupportedException();
            }

            public override long Seek(long offset, SeekOrigin origin)
            {
                throw new NotSupportedException();
            }

            public override void SetLength(long value)
            {
                throw new NotSupportedException();
            }

            public override void Write(byte[] buffer, int offset, int count)
            {
                _bytesAttempted += count;
                if (_bytesAttempted > _limit)
                {
                    throw new ResponseTooLargeException(_bytesAttempted, _limit);
                }
                _inner.Write(buffer, offset, count);
            }

            public byte[] ToArray()
            {
                return _inner.ToArray();
            }
        }
    }
}
