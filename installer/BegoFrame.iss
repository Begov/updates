; Установщик BegoFrame (Inno Setup 6). Собирается tools/build.py — вручную не запускать:
;   ISCC /DAppVersion=1.2.3 /DStageDir=<build\stage\BegoFrame> /DOutputDir=<dist> /DIconFile=<icon.ico> BegoFrame.iss
;
; • ставится без прав администратора (по умолчанию %LOCALAPPDATA%\Programs\BegoFrame, папку можно сменить);
; • повторный запуск поверх установки = обновление: код заменяется, данные пользователя остаются;
; • программа сама запускает установщик тихо: /SILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RELAUNCH=1.
;
; Данные пользователя (projects, inbox, runtime, userdata, engine\.env, engine\instructions,
; engine\templates, engine\logs) установщик не кладёт и не удаляет — ни при обновлении, ни при удалении.

#ifndef AppVersion
  #error AppVersion не задан — собирайте через tools/build.py
#endif

[Setup]
AppId={{6C1E2B7A-5D0B-4E8E-9C4F-BE60F0A1B7E3}
AppName=BegoFrame
AppVersion={#AppVersion}
AppVerName=BegoFrame {#AppVersion}
AppPublisher=Begov
AppPublisherURL=https://github.com/Begov/updates
AppUpdatesURL=https://github.com/Begov/updates/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\BegoFrame
DefaultGroupName=BegoFrame
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=BegoFrame-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\BegoFrame.exe
UninstallDisplayName=BegoFrame
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ru.LaunchApp=Запустить BegoFrame
en.LaunchApp=Launch BegoFrame
ru.NoFfmpeg=Для склейки видео нужен FFmpeg (ffmpeg и ffprobe в PATH), а он не найден.%n%nУстановите его командой:%n    winget install Gyan.FFmpeg%nили с сайта ffmpeg.org — затем перезапустите BegoFrame.
en.NoFfmpeg=Rendering needs FFmpeg (ffmpeg and ffprobe in PATH), and it was not found.%n%nInstall it with:%n    winget install Gyan.FFmpeg%nor from ffmpeg.org, then restart BegoFrame.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Код прежней версии убирается целиком, чтобы не осталось удалённых модулей и старых .py
; (исходник рядом с .pyc имел бы приоритет). Папки с данными пользователя здесь не перечислены.
Type: filesandordirs; Name: "{app}\studio"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\engine\units"
Type: filesandordirs; Name: "{app}\engine\helpers"
Type: filesandordirs; Name: "{app}\engine\schemas"
Type: filesandordirs; Name: "{app}\engine\instructions_default"
Type: filesandordirs; Name: "{app}\engine\__pycache__"
Type: files; Name: "{app}\engine\*.py"
Type: files; Name: "{app}\engine\*.pyc"
Type: files; Name: "{app}\bego.py"
Type: files; Name: "{app}\bego.pyc"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\BegoFrame"; Filename: "{app}\BegoFrame.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\BegoFrame"; Filename: "{app}\BegoFrame.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\BegoFrame.exe"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent
; обновление из программы: после тихой установки — снова запустить
Filename: "{app}\BegoFrame.exe"; Flags: nowait; Check: IsRelaunch

[UninstallDelete]
; кэш байткода Python, созданный во время работы; данные пользователя не трогаем
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\studio"
Type: filesandordirs; Name: "{app}\engine\units"
Type: filesandordirs; Name: "{app}\engine\helpers"

[Code]
function IsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function HasFfmpeg: Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C ffmpeg -version >NUL 2>&1', '', SW_HIDE, ewWaitUntilTerminated, Code)
            and (Code = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WizardSilent) and (not HasFfmpeg) then
    MsgBox(CustomMessage('NoFfmpeg'), mbInformation, MB_OK);
end;
