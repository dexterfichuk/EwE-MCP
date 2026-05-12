; ---------------------------------------------------------------------------
; EwE MCP Plugin — Inno Setup 6 Installer
; ---------------------------------------------------------------------------
; Builds a self-contained installer that drops the MCP plugin DLLs into an
; existing Ecopath with Ecosim (EwE) installation and creates a default
; configuration file in %APPDATA%.
;
; Usage
;   Compile with Inno Setup 6:  iscc installer\EwE_MCP_Plugin.iss
;   The DLLs listed in [Files] must already exist next to this script
;   (or adjust Source paths to point at your build output directory).
; ---------------------------------------------------------------------------

#define AppName      "EwE MCP Plugin"
#define AppVersion   GetVersionNumbersString("EwE_MCP_Plugin.dll", "0.1.0")
#define AppPublisher "EwE-MCP Contributors"
#define AppURL       "https://github.com/dexterfichuk/EwE-MCP"
#define AppId        "{{B3E4F1A2-7C9D-4E8A-B5F6-2D1E3C4A5B6D}"

[Setup]
AppName               = {#AppName}
AppVersion            = {#AppVersion}
AppVerName            = {#AppName} {#AppVersion}
AppPublisher          = {#AppPublisher}
AppPublisherURL       = {#AppURL}
AppSupportURL         = {#AppURL}
AppId                 = {#AppId}

; We do not ship EwE itself — we install into an existing EwE directory.
DefaultDirName        = {code:GetDefaultEwEDir}
DefaultGroupName      = {#AppName}
UninstallDisplayName  = {#AppName} {#AppVersion}

; The installer itself goes to Program Files so it shows in Add/Remove Programs.
DefaultUninstallDir   = {autopf}\{#AppName}

; The "app" directory IS the EwE installation directory — we only write under
; its Plugins\ subfolder and never touch other EwE files.
UsePreviousAppDir     = yes
DirExistsWarning      = yes

; Output
OutputBaseFilename    = EwE_MCP_Plugin_{#AppVersion}_Setup
OutputDir             = installer\output
Compression           = lzma2/ultra64
SolidCompression      = yes
LZMANumBlockThreads   = 4

; Modern Inno Setup 6 settings
WizardStyle           = modern
PrivilegesRequired    = lowest
PrivilegesRequiredOverridesAllowed = dialog

; Architecture — EwE 6.7 is 32-bit only
ArchitecturesAllowed  = x86
ArchitecturesInstallIn64BitMode = x64

; Misc
ShowLanguageDialog    = no
LanguageDetectionMethod = none

; Disable program-group page — we only add an uninstall entry
DisableProgramGroupPage = yes

; Ensure the config directory exists on install
UsedUserAreasInSystem = yes

; We do not create a main executable — the plugin is loaded by EwE
Uninstallable         = yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ---------------------------------------------------------------------------
; Types / Components — single install, no choices needed
; ---------------------------------------------------------------------------
[Types]
Name: "full"; Description: "Full installation"

[Components]
Name: "plugin"; Description: "EwE MCP Plugin files"; Types: full; Flags: fixed

; ---------------------------------------------------------------------------
; Files — copy DLLs into the EwE Plugins directory
; ---------------------------------------------------------------------------
; Source paths are relative to the .iss file.  Adjust if your build output
; lives elsewhere (e.g. ..\bin\Release\).
[Files]
; Primary plugin DLL
DestDir: "{app}\Plugins"; Source: "EwE_MCP_Plugin.dll"; Flags: ignoreversion; Components: plugin
; JSON dependency — Newtonsoft.Json is not bundled with EwE
DestDir: "{app}\Plugins"; Source: "Newtonsoft.Json.dll"; Flags: ignoreversion onlyifdoesntexist; Components: plugin

; ---------------------------------------------------------------------------
; Config file — created in %APPDATA% only when missing
; ---------------------------------------------------------------------------
; The AfterInstall helper writes default values the first time.
[Files]
DestDir: "{userappdata}\Ecopath with Ecosim"; Source: "EwE_MCP_Plugin.cfg"; \
  Flags: onlyifdoesntexist confirmoverwrite; Check: ConfigFileMissing

; ---------------------------------------------------------------------------
; Icons — just a link to the README / GitHub
; ---------------------------------------------------------------------------
[Icons]
Name: "{group}\GitHub Repository"; Filename: "{#AppURL}"

; ---------------------------------------------------------------------------
; Registry — for Add/Remove Programs
; ---------------------------------------------------------------------------
[Registry]
; Nothing extra — Inno Setup manages the uninstall key via AppId

; ---------------------------------------------------------------------------
; Uninstall — only remove *our* files, never other plugins
; ---------------------------------------------------------------------------
[UninstallDelete]
Type: files; Name: "{app}\Plugins\EwE_MCP_Plugin.dll"
Type: files; Name: "{app}\Plugins\Newtonsoft.Json.dll"
Type: files; Name: "{userappdata}\Ecopath with Ecosim\EwE_MCP_Plugin.cfg"
; Remove the config directory only if it is now empty (i.e. EwE itself
; hasn't written other files there — which it will, so this is a no-op in
; practice, but keeps things tidy for a standalone test install).
Type: dirifempty; Name: "{userappdata}\Ecopath with Ecosim"

; ---------------------------------------------------------------------------
; Code — helper functions
; ---------------------------------------------------------------------------
[Code]

/// Return the standard EwE 6.7 install path if it exists, otherwise fall
/// back to the root of Program Files so the user can browse.
function GetDefaultEwEDir(Param: string): string;
var
  StandardPath: string;
begin
  StandardPath := ExpandConstant('{autopf}\Ecopath with Ecosim 6.7.0.18865_32-bit');
  if DirExists(StandardPath) then
    Result := StandardPath
  else
    Result := ExpandConstant('{autopf}');
end;

/// Check whether the config file already exists so we skip overwriting it.
function ConfigFileMissing: Boolean;
begin
  Result := not FileExists(ExpandConstant('{userappdata}\Ecopath with Ecosim\EwE_MCP_Plugin.cfg'));
end;

/// Validate that the chosen directory looks like an EwE installation.
function NextButtonClick(CurPageID: Integer): Boolean;
var
  EwEExe: string;
begin
  Result := True;

  if CurPageID = wpSelectDir then
  begin
    EwEExe := ExpandConstant('{app}\EwE6.exe');
    if not FileExists(EwEExe) then
    begin
      if MsgBox(
        'The selected directory does not appear to contain EwE6.exe. '#13#10 +
        'The plugin will still be installed, but it may not load correctly '#13#10 +
        'unless this is a valid Ecopath with Ecosim installation.'#13#10#13#10 +
        'Continue anyway?',
        mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
  end;
end;

/// After the config file is installed, write sensible defaults if it is
/// empty / newly created.
procedure CurStepChanged(CurStep: TSetupStep);
var
  CfgPath: string;
  Lines: TStringList;
begin
  if CurStep <> ssPostInstall then
    Exit;

  CfgPath := ExpandConstant('{userappdata}\Ecopath with Ecosim\EwE_MCP_Plugin.cfg');
  if not FileExists(CfgPath) then
    Exit;

  // Only write defaults when the file is empty (just created by the
  // onlyifdoesntexist copy above, or by a prior install that left it blank).
  Lines := TStringList.Create();
  try
    Lines.LoadFromFile(CfgPath);
    if Lines.Count = 0 then
    begin
    Lines.Add('# EwE MCP Plugin configuration');
    Lines.Add('# These values can also be overridden via environment variables.');
    Lines.Add('');
    Lines.Add('[EwE_MCP]');
    Lines.Add('host=127.0.0.1');
    Lines.Add('port=9877');
    Lines.Add('token=ewe-mcp-local-dev');
      Lines.SaveToFile(CfgPath);
    end;
  finally
    Lines.Free();
  end;
end;
