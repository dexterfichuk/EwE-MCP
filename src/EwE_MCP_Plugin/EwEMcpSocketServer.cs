using System;
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
        private static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hWnd);

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        private const uint GW_OWNER = 4;
        private const uint BM_CLICK = 0x00F5;
        private const int IDOK = 1;
        private const int IDYES = 6;
        private static readonly int _currentPid = System.Diagnostics.Process.GetCurrentProcess().Id;

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
                    var responseJson = response.ToString(Formatting.None);
                    var responseBytes = Encoding.UTF8.GetByteCount(responseJson);
                    if (responseBytes > _config.MaxMessageBytes)
                    {
                        _auditLog.Add(request?.Type ?? "unknown", "response_oversized", $"Response {responseBytes} bytes exceeds limit {_config.MaxMessageBytes}");
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
                        responseJson = oversizedResponse.ToString(Formatting.None);
                    }
                    await WriteFrameAsync(stream, responseJson, token).ConfigureAwait(false);
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
                            tcs.TrySetResult(new JObject
                            {
                                ["id"] = request.Id,
                                ["status"] = "success",
                                ["result"] = result ?? new JObject()
                            });
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

        private static async Task WriteFrameAsync(NetworkStream stream, string json, CancellationToken token)
        {
            var payload = Encoding.UTF8.GetBytes(json);
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
            _dialogWatcherCts = new CancellationTokenSource();
            _dialogWatcherLoop = Task.Run(() => DialogWatcherLoopAsync(_dialogWatcherCts.Token));
        }

        private void StopDialogWatcher()
        {
            try
            {
                _dialogWatcherCts?.Cancel();
                _dialogWatcherLoop?.Wait(2000);
            }
            catch
            {
            }
            finally
            {
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
                    var dismissed = DismissBlockingDialogs();
                    if (dismissed > 0)
                    {
                        _auditLog.Add("dialog_watcher", "dismissed", $"{dismissed} blocking dialog(s) auto-dismissed");
                    }
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch
                {
                }
            }
        }

        private static int DismissBlockingDialogs()
        {
            var dismissed = 0;
            EnumWindows((hWnd, _) =>
            {
                GetWindowThreadProcessId(hWnd, out var pid);
                if (pid != _currentPid)
                {
                    return true;
                }

                if (!IsWindowVisible(hWnd))
                {
                    return true;
                }

                var owner = GetWindow(hWnd, GW_OWNER);
                if (owner == IntPtr.Zero)
                {
                    return true;
                }

                var className = new StringBuilder(256);
                GetClassName(hWnd, className, className.Capacity);
                var cls = className.ToString();

                var title = new StringBuilder(512);
                GetWindowText(hWnd, title, title.Capacity);
                var titleText = title.ToString();

                if (cls == "#32770")
                {
                    var btn = FindWindowEx(hWnd, IntPtr.Zero, "Button", null);
                    var clicked = false;
                    while (btn != IntPtr.Zero)
                    {
                        var btnId = GetDlgCtrlID(btn);
                        if (btnId == IDYES || btnId == IDOK)
                        {
                            SetForegroundWindow(hWnd);
                            SendMessage(btn, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
                            clicked = true;
                            EwEMcpPlugin.EwEMcpFileLog.Info($"Dialog watcher dismissed Win32 dialog '{titleText}' (class #32770, button ID {btnId})");
                            dismissed++;
                            break;
                        }
                        btn = FindWindowEx(hWnd, btn, "Button", null);
                    }
                    if (!clicked)
                    {
                        SendMessage(hWnd, 0x0111, (IntPtr)IDOK, IntPtr.Zero);
                        EwEMcpPlugin.EwEMcpFileLog.Info($"Dialog watcher sent WM_COMMAND/IDOK to Win32 dialog '{titleText}' (class #32770)");
                        dismissed++;
                    }
                }
                else
                {
                    var yesBtn = FindWindowEx(hWnd, IntPtr.Zero, null, "&Yes");
                    if (yesBtn == IntPtr.Zero)
                    {
                        yesBtn = FindWindowEx(hWnd, IntPtr.Zero, null, "Yes");
                    }
                    if (yesBtn != IntPtr.Zero)
                    {
                        SetForegroundWindow(hWnd);
                        SendMessage(yesBtn, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
                        EwEMcpPlugin.EwEMcpFileLog.Info($"Dialog watcher dismissed WinForms dialog '{titleText}' by clicking Yes");
                        dismissed++;
                    }
                    else
                    {
                        var okBtn = FindWindowEx(hWnd, IntPtr.Zero, null, "OK");
                        if (okBtn == IntPtr.Zero)
                        {
                            okBtn = FindWindowEx(hWnd, IntPtr.Zero, null, "&OK");
                        }
                        if (okBtn != IntPtr.Zero)
                        {
                            SetForegroundWindow(hWnd);
                            SendMessage(okBtn, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
                            EwEMcpPlugin.EwEMcpFileLog.Info($"Dialog watcher dismissed WinForms dialog '{titleText}' by clicking OK");
                            dismissed++;
                        }
                    }
                }

                return true;
            }, IntPtr.Zero);
            return dismissed;
        }

        [DllImport("user32.dll")]
        private static extern int GetDlgCtrlID(IntPtr hWnd);
    }
}
