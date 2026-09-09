#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=
#endif
#ifndef SourceMsi
  #error SourceMsi must be supplied with /DSourceMsi=
#endif
#ifndef OutputDir
  #error OutputDir must be supplied with /DOutputDir=
#endif

[Setup]
AppName=Huifa Media Downloader
AppVersion={#AppVersion}
AppPublisher=Huifa
DefaultDirName={autopf}\Huifa_Media_Downloader
DisableWelcomePage=no
DisableDirPage=no
DisableReadyPage=no
DisableFinishedPage=no
CreateAppDir=yes
Uninstallable=no
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=HuifaMediaDownloader-Setup
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Huifa
VersionInfoDescription=Huifa Media Downloader Setup

[Files]
Source: "{#SourceMsi}"; DestDir: "{tmp}"; DestName: "HuifaMediaDownloader.msi"; Flags: dontcopy

[Run]
Filename: "{app}\Huifa Media Downloader.exe"; Description: "Launch Huifa Media Downloader"; Flags: nowait postinstall skipifsilent; Check: MsiInstallSucceeded

[Code]
var
  Installed: Boolean;

function MsiInstallSucceeded: Boolean;
begin
  Result := Installed;
end;

function MsiParameters: String;
begin
  Result :=
    '/i "' + ExpandConstant('{tmp}\HuifaMediaDownloader.msi') + '" ' +
    'VELOPACK_INSTALLDIR="' + ExpandConstant('{app}') + '" ' +
    'ALLUSERS=1 MSIINSTALLPERUSER="" /qn /norestart';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep <> ssInstall then
    exit;

  WizardForm.StatusLabel.Caption := 'Installing Huifa Media Downloader...';
  ExtractTemporaryFile('HuifaMediaDownloader.msi');
  if not Exec(
    ExpandConstant('{sys}\msiexec.exe'),
    MsiParameters,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    RaiseException('Windows Installer could not be started.');
  if (ResultCode <> 0) and (ResultCode <> 3010) then
    RaiseException(Format('Installation failed (Windows Installer error %d).', [ResultCode]));
  Installed := True;
end;
