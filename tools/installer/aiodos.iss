; Installeur Windows d'AIODOS (Inno Setup 6). Construit par tools/build_installer.py, qui
; prépare les fichiers (Python embarqué, code, compagnon, moteurs) et passe :
;   /DAppVersion=…  /DStage=…  /DOutDir=…  /DRepo=…  /DIcon=… (icône des raccourcis, nommée
;   d'après son contenu : Windows garde en cache l'icône d'un chemin déjà vu), et ce que
;   prépare tools/installer_pages.py : /DLegalFr=… /DLegalEn=… (la charte et les conditions
;   d'utilisation, en RTF), /DWizardImages=… /DWizardSmallImages=… (le logo, de 100 à 250 %).
; L'assistant, sombre comme AIODOS : l'accueil (logo et présentation), puis la charte et les
; conditions d'utilisation, à accepter pour installer (demandé par l'auteur le 27/10/2026).
; Installation pour l'utilisateur seul, sans droits administrateur, dans
; %LOCALAPPDATA%\Programs\AIODOS ; les données vont dans %LOCALAPPDATA%\AIODOS
; (aiodos/paths.py, installation.json) et survivent à une nouvelle installation.
; Le dossier n'est pas proposé au choix, et un dossier hors du profil (/DIR=C:\AIODOS…)
; est refusé : sous C:\ ou sur un autre disque, il hériterait de « Utilisateurs
; authentifiés : modification », et tout autre compte du PC pourrait remplacer le Python
; ou le code qu'AIODOS lance (PrepareToInstall). Une ancienne installation faite ailleurs
; n'est pas reprise : AIODOS s'installe à sa place habituelle, le raccourci du démarrage
; avec Windows qui la lançait est refait sur la nouvelle (MoveStartupShortcut), et l'ancien
; dossier est signalé à la fin, pour être supprimé.

#ifndef Stage
  #error "Stage n'est pas défini : construire avec tools/build_installer.py"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Icon
  #define Icon "logo.ico"
#endif
#ifndef OutDir
  #define OutDir "dist"
#endif
#ifndef LegalFr
  #error "LegalFr n'est pas défini : construire avec tools/build_installer.py"
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
AppComments={cm:AppComments}
DefaultDirName={autopf}\AIODOS
DisableDirPage=yes
UsePreviousAppDir=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=AIODOS-Setup-{#AppVersion}
SetupIconFile={#Repo}\assets\logo.ico
UninstallDisplayIcon={app}\app\assets\{#Icon}
UninstallDisplayName=AIODOS
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern dark
DisableWelcomePage=no
WizardImageFile={#WizardImages}
WizardSmallImageFile={#WizardSmallImages}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809 et plus : WebView2 (fenêtre d'AIODOS) et pythonnet
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no

[Languages]
; La langue de Windows choisit ; ni française ni anglaise : la première, l'anglais
; La charte et les conditions d'utilisation (aiodos/legal), à accepter : à la place de la
; licence Apache 2.0, qu'elles citent et qui est installée avec AIODOS (LICENSE)
Name: "en"; MessagesFile: "compiler:Default.isl"; LicenseFile: "{#LegalEn}"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"; LicenseFile: "{#LegalFr}"

[Messages]
; L'accueil : le texte de présentation, à côté du logo
fr.WelcomeLabel1=Bienvenue dans AIODOS
fr.WelcomeLabel2=AIODOS fait tourner l'intelligence artificielle sur tes propres machines et les fait travailler ensemble : tes PC, ton téléphone et, si tu le veux, ceux de tes amis et du réseau.%n%nConverse avec des modèles ouverts, interroge tes documents, crée des images et des vidéos, sans abonnement ni cloud : tes données restent chez toi. Un modèle trop gros pour une seule machine se répartit entre plusieurs.%n%nCet assistant va installer [name/ver] sur ton ordinateur.
en.WelcomeLabel1=Welcome to AIODOS
en.WelcomeLabel2=AIODOS runs artificial intelligence on your own machines and makes them work together: your PCs, your phone and, if you want, those of your friends and of the network.%n%nChat with open models, search your documents, create images and videos, with no subscription and no cloud: your data stays with you. A model too large for one machine is split across several.%n%nThis wizard will install [name/ver] on your computer.
; La page de licence : la charte et les conditions d'utilisation
fr.WizardLicense=Charte et conditions d'utilisation
fr.LicenseLabel=Lis-les avant de continuer.
fr.LicenseLabel3=La charte dit l'esprit dans lequel on utilise AIODOS ensemble ; les conditions d'utilisation, ses règles. Il faut les accepter pour installer AIODOS.
fr.LicenseAccepted=J'&accepte la charte et les conditions d'utilisation
fr.LicenseNotAccepted=Je &refuse
en.WizardLicense=Charter and terms of use
en.LicenseLabel=Please read them before continuing.
en.LicenseLabel3=The charter sets out the spirit in which we use AIODOS together; the terms of use, its rules. You must accept them to install AIODOS.
en.LicenseAccepted=I &accept the charter and the terms of use
en.LicenseNotAccepted=I &do not accept

[CustomMessages]
fr.OllamaTask=Télécharger et installer Ollama, le moteur des conversations (depuis ollama.com, environ 1 Go)
en.OllamaTask=Download and install Ollama, the chat engine (from ollama.com, about 1 GB)
fr.OllamaFailed=Ollama n'a pas pu être téléchargé. Tu pourras l'installer plus tard depuis https://ollama.com/download : AIODOS s'en servira dès qu'il tournera.
en.OllamaFailed=Ollama could not be downloaded. You can install it later from https://ollama.com/download: AIODOS will use it as soon as it runs.
fr.OllamaInstalling=Installation d'Ollama, le moteur des conversations : une à deux minutes. Sa fenêtre s'ouvrira ensuite d'elle-même ; AIODOS s'en sert tout seul, tu n'as rien à y faire.
en.OllamaInstalling=Installing Ollama, the chat engine: one or two minutes. Its window will then open by itself; AIODOS uses it on its own, nothing to do there.
fr.FinishedLabel=AIODOS est installé. Sa fenêtre s'ouvre dans quelques secondes si la case ci-dessous reste cochée ; le premier démarrage est un peu plus long.
en.FinishedLabel=AIODOS is installed. Its window opens in a few seconds if the box below stays checked; the first start takes a little longer.
fr.NoWebView2=Le moteur d'affichage WebView2 de Microsoft semble absent : la fenêtre d'AIODOS en a besoin. Il est livré avec Windows 11 et les Windows 10 à jour ; sinon : https://go.microsoft.com/fwlink/p/?LinkId=2124703
en.NoWebView2=Microsoft's WebView2 runtime seems to be missing: the AIODOS window needs it. It ships with Windows 11 and up-to-date Windows 10; otherwise: https://go.microsoft.com/fwlink/p/?LinkId=2124703
fr.DeleteData=Supprimer aussi tes données AIODOS (conversations, bibliothèques, réglages, images, modèles téléchargés) ?
en.DeleteData=Also delete your AIODOS data (conversations, libraries, settings, images, downloaded models)?
fr.AppComment=AIODOS, l'atelier d'IA local
en.AppComment=AIODOS, the local AI workshop
fr.AppComments=Atelier d'IA local, réparti entre tes machines
en.AppComments=Local AI workshop, shared across your machines
fr.BackgroundShortcut=AIODOS, en arrière-plan
en.BackgroundShortcut=AIODOS, in the background
fr.OutsideProfile=AIODOS s'installe dans ton dossier personnel (%1). Le dossier demandé, %2, est hors de ton profil : les autres comptes de ce PC pourraient y modifier le programme qu'AIODOS lance. Relance l'installeur sans choisir de dossier.
en.OutsideProfile=AIODOS installs in your personal folder (%1). The requested folder, %2, is outside your profile: other accounts on this PC could modify the program AIODOS runs. Run the installer again without choosing a folder.
fr.OldFolder=Une ancienne installation d'AIODOS se trouvait dans %1. Elle n'est plus utilisée : AIODOS est maintenant installé dans %2 (tes données ne changent pas de place). Le démarrage avec Windows, s'il était activé, lance maintenant cette nouvelle installation. Tu peux supprimer l'ancien dossier.
en.OldFolder=An old AIODOS installation was in %1. It is no longer used: AIODOS is now installed in %2 (your data stays where it was). Starting with Windows, if it was enabled, now runs this new installation. You can delete the old folder.

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
Name: "{userprograms}\AIODOS"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m aiodos"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\{#Icon}"; AppUserModelID: "Arkane.Aiodos"; Comment: "{cm:AppComment}"
Name: "{userdesktop}\AIODOS"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m aiodos"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\{#Icon}"; AppUserModelID: "Arkane.Aiodos"; Comment: "{cm:AppComment}"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m aiodos"; WorkingDir: "{app}\app"; Description: "{cm:LaunchProgram,AIODOS}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Fichiers compilés par Python à l'usage (__pycache__), absents de l'installation
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}_is1';

var
  DownloadPage: TDownloadWizardPage;
  PreviousDir: String;

function InitializeSetup: Boolean;
begin
  // Dossier d'une installation précédente, lu avant que celle-ci ne le remplace
  if not RegQueryStringValue(HKCU, UninstallKey, 'Inno Setup: App Path', PreviousDir) then
    PreviousDir := '';
  Result := True;
end;

// Dans le profil de l'utilisateur (%LOCALAPPDATA%) : dossier à lui seul
function InsideProfile(Dir: String): Boolean;
var
  Profile: String;
begin
  Profile := Lowercase(AddBackslash(ExpandConstant('{localappdata}')));
  Result := (Pos('..', Dir) = 0) and (Pos(Profile, Lowercase(AddBackslash(Dir))) = 1);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  // (une ligne de [Code] ne doit pas commencer par un crochet : Inno y verrait une section)
  if not InsideProfile(ExpandConstant('{app}')) then
    Result := FmtMessage(CustomMessage('OutsideProfile'), [
      ExpandConstant('{localappdata}\Programs\AIODOS'), ExpandConstant('{app}')]);
end;

// Programme que vise un raccourci (vide s'il est illisible)
function ShortcutTarget(Path: String): String;
var
  Shell, Link: Variant;
begin
  Result := '';
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(Path);
    Result := Link.TargetPath;
  except
    Result := '';
  end;
end;

// Raccourci du démarrage avec Windows (node/autostart.py) qui lance encore l'ancienne
// installation, hors du profil : il viserait chaque jour un dossier que les autres comptes
// peuvent modifier. Il est refait sur celle-ci, ou retiré s'il ne peut pas l'être.
procedure MoveStartupShortcut;
var
  Link: String;
begin
  Link := ExpandConstant('{userstartup}\AIODOS.lnk');
  if not FileExists(Link) then
    exit;
  if Pos(Lowercase(AddBackslash(PreviousDir)), Lowercase(ShortcutTarget(Link))) <> 1 then
    exit;
  try
    CreateShellLink(Link, CustomMessage('BackgroundShortcut'), ExpandConstant('{app}\runtime\pythonw.exe'),
      '-m aiodos --background', ExpandConstant('{app}\app'),
      ExpandConstant('{app}\app\assets\{#Icon}'), 0, 7);
  except
    DeleteFile(Link);
  end;
end;

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

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption := CustomMessage('FinishedLabel');
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
    begin
      // Installation silencieuse d'Ollama, qui prend du temps : dit ici, avec une barre
      // animée, au lieu d'un assistant qui semble figé
      WizardForm.StatusLabel.Caption := CustomMessage('OllamaInstalling');
      WizardForm.FilenameLabel.Caption := '';
      WizardForm.ProgressGauge.Style := npbstMarquee;
      try
        Exec(ExpandConstant('{tmp}\OllamaSetup.exe'), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
          '', SW_SHOW, ewWaitUntilTerminated, Code);
      finally
        WizardForm.ProgressGauge.Style := npbstNormal;
      end;
    end;
    if not WebView2Present then
      SuppressibleMsgBox(CustomMessage('NoWebView2'), mbInformation, MB_OK, IDOK);
    if (PreviousDir <> '') and
       (CompareText(RemoveBackslash(PreviousDir), RemoveBackslash(ExpandConstant('{app}'))) <> 0) then
    begin
      MoveStartupShortcut;
      if DirExists(PreviousDir) then
        SuppressibleMsgBox(FmtMessage(CustomMessage('OldFolder'), [
          PreviousDir, ExpandConstant('{app}')]), mbInformation, MB_OK, IDOK);
    end;
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
