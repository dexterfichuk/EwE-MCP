[Setup]
AppName=EwE MCP
AppVersion=0.1.0
DefaultDirName={autopf}\EwE-MCP
DefaultGroupName=EwE MCP
OutputDir=..\dist
OutputBaseFilename=EwE-MCP-Setup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=lowest
UninstallDisplayName=EwE MCP
UninstallDisplayIcon={app}\ewe-mcp.exe

[Files]
; The Python FastMCP Server (Standalone Executable Directory)
Source: "..\dist\ewe-mcp\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; The C# Plugin (Installed into the EwE Plugins directory, selected by user in code)
; 32-bit EwE
Source: "..\src\EwE_MCP_Plugin\bin\Release\net48\x86\EwE_MCP_Plugin.dll"; DestDir: "{code:GetEwEDir}\Plugins"; Flags: ignoreversion; Check: not IsEwE64Bit
Source: "..\src\EwE_MCP_Plugin\bin\Release\net48\x86\Newtonsoft.Json.dll"; DestDir: "{code:GetEwEDir}\Plugins"; Flags: ignoreversion; Check: not IsEwE64Bit
; 64-bit EwE
Source: "..\src\EwE_MCP_Plugin\bin\Release\net48\x64\EwE_MCP_Plugin.dll"; DestDir: "{code:GetEwEDir}\Plugins"; Flags: ignoreversion; Check: IsEwE64Bit
Source: "..\src\EwE_MCP_Plugin\bin\Release\net48\x64\Newtonsoft.Json.dll"; DestDir: "{code:GetEwEDir}\Plugins"; Flags: ignoreversion; Check: IsEwE64Bit

; Sample plugin config file
Source: "EwE_MCP_Plugin.cfg"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: files; Name: "{app}\AGENT_SETUP.md"
Type: files; Name: "{app}\agent-dir.txt"

[Run]
Filename: "https://opencode.ai/download"; Description: "Download OpenCode CLI to connect to EwE MCP"; Flags: postinstall nowait shellexec unchecked

[Code]
var
  EwEDirPage: TInputDirWizardPage;
  AgentDirPage: TInputDirWizardPage;
  ArchPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  EwEDirPage := CreateInputDirPage(wpSelectDir,
    'Select EwE Installation Directory', 'Where is Ecopath with Ecosim installed?',
    'Select the folder in which Ecopath with Ecosim is installed (it must contain ewe6.exe).',
    False, '');
  EwEDirPage.Add('');

  // Try to find a default installation path
  if DirExists(ExpandConstant('{pf32}\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit')) then
    EwEDirPage.Values[0] := ExpandConstant('{pf32}\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit')
  else if DirExists(ExpandConstant('{pf32}\Ecopath with Ecosim')) then
    EwEDirPage.Values[0] := ExpandConstant('{pf32}\Ecopath with Ecosim')
  else
    EwEDirPage.Values[0] := ExpandConstant('{pf32}');

  ArchPage := CreateInputOptionPage(EwEDirPage.ID,
    'Select EwE Architecture', 'Is your EwE installation 32-bit or 64-bit?',
    'Select the architecture of your Ecopath with Ecosim installation. This determines which plugin DLL is installed.',
    False, False);
  ArchPage.Add('32-bit (x86)');
  ArchPage.Add('64-bit (x64)');
  ArchPage.SelectedValueIndex := 0;

  AgentDirPage := CreateInputDirPage(ArchPage.ID,
    'Select Agent Instructions Directory', 'Where should agent setup instructions be saved?',
    'An AGENT_SETUP.md file will be created in this folder so your AI agent knows the installation paths.',
    False, '');
  AgentDirPage.Add('');
  AgentDirPage.Values[0] := ExpandConstant('{userdocs}\EwE-MCP-Agent');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = EwEDirPage.ID then
  begin
    if not FileExists(EwEDirPage.Values[0] + '\ewe6.exe') then
    begin
      MsgBox('Could not find ewe6.exe in the selected folder. Please select a valid EwE installation directory.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function GetEwEDir(Param: String): String;
begin
  Result := EwEDirPage.Values[0];
end;

function IsEwE64Bit: Boolean;
begin
  Result := ArchPage.SelectedValueIndex = 1;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppPath, EwEPath, AgentDir, FilePath, Content: string;
begin
  if CurStep = ssPostInstall then
  begin
    AppPath := ExpandConstant('{app}');
    EwEPath := EwEDirPage.Values[0];
    AgentDir := AgentDirPage.Values[0];
    FilePath := AgentDir + '\AGENT_SETUP.md';

    if not DirExists(AgentDir) then
      ForceDirectories(AgentDir);

    SaveStringToFile(AppPath + '\agent-dir.txt', AgentDir, False);

    Content :=
      '# EwE MCP — Agent Setup' + #13#10 +
      '' + #13#10 +
      '## Server Location' + #13#10 +
      'The EwE MCP server is installed at:' + #13#10 +
      '' + #13#10 +
      '    ' + AppPath + #13#10 +
      '' + #13#10 +
      'The MCP protocol requires a persistent server session. Start it once' + #13#10 +
      'and leave it running — do not restart between commands.' + #13#10 +
      '' + #13#10 +
      'Run this command to start it:' + #13#10 +
      '' + #13#10 +
      '    set EWE_MCP_HOST=127.0.0.1' + #13#10 +
      '    set EWE_MCP_PORT=9877' + #13#10 +
      '    set EWE_MCP_TOKEN=ewe-mcp-local-dev' + #13#10 +
      '    "' + AppPath + '\ewe-mcp.exe"' + #13#10 +
      '' + #13#10 +
      '## EwE Plugin' + #13#10 +
      'EwE is installed at:' + #13#10 +
      '' + #13#10 +
      '    ' + EwEPath + #13#10 +
      '' + #13#10 +
      'The MCP plugin DLL was installed to:' + #13#10 +
      '' + #13#10 +
      '    ' + EwEPath + '\Plugins\EwE_MCP_Plugin.dll' + #13#10 +
      '' + #13#10 +
      '## Plugin Config' + #13#10 +
      'If needed, create a config file at:' + #13#10 +
      '' + #13#10 +
      '    %APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.cfg' + #13#10 +
      '' + #13#10 +
      'With these contents:' + #13#10 +
      '' + #13#10 +
      '    [EwE_MCP]' + #13#10 +
      '    host=127.0.0.1' + #13#10 +
      '    port=9877' + #13#10 +
      '    token=ewe-mcp-local-dev' + #13#10 +
      '' + #13#10 +
      '## Configuring an MCP Host' + #13#10 +
      '' + #13#10 +
      '### Claude Desktop' + #13#10 +
      '' + #13#10 +
      'Add to claude_desktop_config.json:' + #13#10 +
      '' + #13#10 +
      '    {' + #13#10 +
      '      "mcpServers": {' + #13#10 +
      '        "ewe": {' + #13#10 +
      '          "command": "' + AppPath + '\\ewe-mcp.exe",' + #13#10 +
      '          "env": {' + #13#10 +
      '            "EWE_MCP_HOST": "127.0.0.1",' + #13#10 +
      '            "EWE_MCP_PORT": "9877",' + #13#10 +
      '            "EWE_MCP_TOKEN": "ewe-mcp-local-dev"' + #13#10 +
      '          }' + #13#10 +
      '        }' + #13#10 +
      '      }' + #13#10 +
      '    }' + #13#10 +
      '' + #13#10 +
       'Replace the backslashes with double backslashes in the actual JSON file.' + #13#10 +
       '' + #13#10 +
       '### opencode' + #13#10 +
       '' + #13#10 +
       'Add to opencode.jsonc:' + #13#10 +
       '' + #13#10 +
       '    {' + #13#10 +
       '      "mcpServers": {' + #13#10 +
       '        "ewe": {' + #13#10 +
       '          "command": "' + AppPath + '\\ewe-mcp.exe",' + #13#10 +
       '          "env": {' + #13#10 +
       '            "EWE_MCP_HOST": "127.0.0.1",' + #13#10 +
       '            "EWE_MCP_PORT": "9877",' + #13#10 +
       '            "EWE_MCP_TOKEN": "ewe-mcp-local-dev"' + #13#10 +
       '          }' + #13#10 +
       '        }' + #13#10 +
       '      }' + #13#10 +
       '    }' + #13#10 +
       '' + #13#10 +
       '## Verification' + #13#10 +
      '' + #13#10 +
      '1. Launch EwE (ewe6.exe)' + #13#10 +
      '2. Check that EwE_MCP_Plugin.log exists in %APPDATA%\Ecopath with Ecosim' + #13#10 +
      '3. Start the server (see "Server Location" above)' + #13#10 +
      '4. The ping tool should return {"status": "ok"}' + #13#10 +
      '' + #13#10 +
      '## Safety Notes' + #13#10 +
      '' + #13#10 +
      '- execute_ewe_code and shutdown_server are disabled by default' + #13#10 +
      '- Set EWE_MCP_ENABLE_EVAL=1 to enable them' + #13#10 +
      '- Do not batch model lifecycle, simulation, export, or mutation commands' + #13#10 +
      '- Prefer save_model_as to a copy path when mutating' + #13#10 +
      '' + #13#10 +
      'For full documentation see ' + AppPath + '\AGENT_SETUP.md or the repository at https://github.com/dexterfichuk/EwE-MCP' + #13#10 +
      '' + #13#10;

    SaveStringToFile(FilePath, Content, False);
    SaveStringToFile(AppPath + '\AGENT_SETUP.md', Content, False);
  end;
end;
