; Installeur Windows d'AIODOS (Inno Setup 6). Construit par tools/build_installer.py, qui
; prépare les fichiers (Python embarqué, code, compagnon, moteurs) et passe :
;   /DAppVersion=…  /DStage=…  /DOutDir=…  /DRepo=…
; Installation pour l'utilisateur seul, sans droits administrateur, dans
; %LOCALAPPDATA%\Programs\AIODOS ; les données vont dans %LOCALAPPDATA%\AIODOS
; (aiodos/paths.py, installation.json) et survivent à une nouvelle installation.

#ifndef Stage
  #error "Stage n'est pas défini : construire avec tools/build_installer.py"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef OutDir
  #define OutDir "dist"
#endif
#ifndef Repo
  #define Repo ".."
#endif

[Setup]
; Identifiant fixe : une nouvelle version remplace la précédente au lieu de s'y ajouter
AppId={{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}
AppName=AIODOS
AppVersion={#AppVersion}
AppVerName=AIODOS {#AppVersion}
AppPublisher=22ème Arkane
AppPublisherURL=https://github.com/AIODOS/aiodos
AppComments=Atelier d'IA local, réparti entre tes machines
DefaultDirName={autopf}\AIODOS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=AIODOS-Setup-{#AppVersion}
SetupIconFile={#Repo}\assets\logo.ico
UninstallDisplayIcon={app}\app\assets\logo.ico
UninstallDisplayName=AIODOS
LicenseFile={#Repo}\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809 et plus : WebView2 (fenêtre d'AIODOS) et pythonnet
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
fr.OllamaTask=Télécharger et installer Ollama, le moteur des conversations (depuis ollama.com, environ 1 Go)
en.OllamaTask=Download and install Ollama, the chat engine (from ollama.com, about 1 GB)
fr.OllamaFailed=Ollama n'a pas pu être téléchargé. Tu pourras l'installer plus tard depuis https://ollama.com/download : AIODOS s'en servira dès qu'il tournera.
en.OllamaFailed=Ollama could not be downloaded. You can install it later from https://ollama.com/download: AIODOS will use it as soon as it runs.
fr.NoWebView2=Le moteur d'affichage WebView2 de Microsoft semble absent : la fenêtre d'AIODOS en a besoin. Il est livré avec Windows 11 et les Windows 10 à jour ; sinon : https://go.microsoft.com/fwlink/p/?LinkId=2124703
en.NoWebView2=Microsoft's WebView2 runtime seems to be missing: the AIODOS window needs it. It ships with Windows 11 and up-to-date Windows 10; otherwise: https://go.microsoft.com/fwlink/p/?LinkId=2124703
fr.DeleteData=Supprimer aussi tes données AIODOS (conversations, bibliothèques, réglages, images, modèles téléchargés) ?
en.DeleteData=Also delete your AIODOS data (conversations, libraries, settings, images, downloaded models)?
fr.AppComment=AIODOS, l'atelier d'IA local
en.AppComment=AIODOS, the local AI workshop

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "ollama"; Description: "{cm:OllamaTask}"; Check: not OllamaInstalled

[InstallDelete]
; Le code d'une installation précédente est remplacé en entier (les données, elles, sont
; ailleurs : %LOCALAPPDATA%\AIODOS)
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"

[Files]
Source: "{#Stage}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Stage}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Même identifiant d'application que la fenêtre (aiodos/__main__.py) : l'icône épinglée et
; la fenêtre ouverte ne font qu'une dans la barre des tâches
Name: "{userprograms}\AIODOS"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m aiodos"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\logo.ico"; AppUserModelID: "Arkane.Aiodos"; Comment: "{cm:AppComment}"
Name: "{userdesktop}\AIODOS"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m aiodos"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\logo.ico"; AppUserModelID: "Arkane.Aiodos"; Comment: "{cm:AppComment}"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m aiodos"; WorkingDir: "{app}\app"; Description: "{cm:LaunchProgram,AIODOS}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Fichiers compilés par Python à l'usage (__pycache__), absents de l'installation
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"

[Code]
var
  DownloadPage: TDownloadWizardPage;

function OllamaInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe'))
    or FileExists(ExpandConstant('{commonpf64}\Ollama\ollama.exe'));
end;

function WebView2Present: Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    or RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    or RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
  Result := Result and (Version <> '') and (Version <> '0.0.0.0');
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
    SetupMessage(msgPreparingDesc), nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('ollama') then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('https://ollama.com/download/OllamaSetup.exe', 'OllamaSetup.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        SuppressibleMsgBox(CustomMessage('OllamaFailed'), mbInformation, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('ollama') and FileExists(ExpandConstant('{tmp}\OllamaSetup.exe')) then
      Exec(ExpandConstant('{tmp}\OllamaSetup.exe'), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
        '', SW_SHOW, ewWaitUntilTerminated, Code);
    if not WebView2Present then
      SuppressibleMsgBox(CustomMessage('NoWebView2'), mbInformation, MB_OK, IDOK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\AIODOS');
    if DirExists(Data) and not UninstallSilent and
       (MsgBox(CustomMessage('DeleteData') + #13#10#13#10 + Data, mbConfirmation,
               MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(Data, True, True, True);
  end;
end;
