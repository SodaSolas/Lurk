using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static void Main()
    {
        string folder = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(folder, @".venv\Scripts\pythonw.exe");
        string app = Path.Combine(folder, "taskbar.pyw");

        try
        {
            if (!File.Exists(python) || !File.Exists(app))
                throw new FileNotFoundException("请将启动器留在项目目录中，并确认 .venv 和 taskbar.pyw 都存在。");

            using (Process.Start(new ProcessStartInfo
            {
                FileName = python,
                Arguments = "\"" + app + "\"",
                WorkingDirectory = folder,
                UseShellExecute = false,
                CreateNoWindow = true
            })) { }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "B 站直播电台", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
