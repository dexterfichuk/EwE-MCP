
import json
from ewe_mcp.client import EwEMCPClient

def probe():
    client = EwEMCPClient()
    if not client.connect():
        print("Failed to connect")
        return
    try:
        print("Connected. Sending list_ecopath_groups...")
        # We manually use the socket to see the raw bytes if it fails
        client.socket.sendall(client.HEADER_STRUCT.pack(len(b'{"id":"1","type":"list_ecopath_groups","params":{},"token":"' + client.token.encode() + b'"}')))
        # Wait, just use the client method but catch the error and look at the socket
        try:
            res = client.send_command("list_ecopath_groups")
            print(f"Success! {len(json.dumps(res))} bytes")
        except Exception as e:
            print(f"Error: {e}")
            # Try to read raw bytes from socket
            client.socket.settimeout(1.0)
            try:
                raw = client.socket.recv(1024)
                print(f"Raw bytes in socket: {raw.hex()} ({raw})")
            except:
                print("Could not read raw bytes")
    finally:
        client.disconnect()

if __name__ == "__main__":
    probe()
