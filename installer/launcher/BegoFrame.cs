// BegoFrame.exe — лаунчер установленной программы.
// Запускает python\pythonw.exe bego.pyc из своей папки и сразу выходит (окна консоли нет).
// Собирается tools/build.py компилятором C# из .NET Framework 4 (есть в каждой Windows 10/11):
//   csc /target:winexe /win32icon:studio\assets\icon.ico /out:BegoFrame.exe BegoFrame.cs
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("BegoFrame")]
[assembly: System.Reflection.AssemblyProduct("BegoFrame")]
[assembly: System.Reflection.AssemblyDescription("BegoFrame video studio")]

static class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "python", "pythonw.exe");
        string entry = Path.Combine(root, "bego.pyc");
        if (!File.Exists(entry))
            entry = Path.Combine(root, "bego.py");
        if (!File.Exists(python) || !File.Exists(entry))
        {
            MessageBox.Show("Файлы программы повреждены или удалены.\nПереустановите BegoFrame.\n\n" +
                            "Program files are damaged or missing. Reinstall BegoFrame.",
                            "BegoFrame", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        var info = new ProcessStartInfo(python)
        {
            Arguments = Quote(entry) + (args.Length > 0 ? " " + string.Join(" ", Array.ConvertAll(args, Quote)) : ""),
            WorkingDirectory = root,
            UseShellExecute = false,
        };
        try
        {
            Process.Start(info);
        }
        catch (Exception ex)
        {
            MessageBox.Show("Не удалось запустить BegoFrame:\n" + ex.Message, "BegoFrame",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }

    static string Quote(string s)
    {
        return "\"" + s.Replace("\"", "\\\"") + "\"";
    }
}
