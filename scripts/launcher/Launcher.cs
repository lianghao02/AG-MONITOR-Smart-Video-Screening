using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace AGMonitorLauncher
{
    static class Program
    {
        [STAThread]
        static void Main()
        {
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory;
                string currentDir = Path.Combine(baseDir, "current");
                string dataDir = Path.Combine(baseDir, "data");
                string logsDir = Path.Combine(dataDir, "logs");
                string yoloConfigDir = Path.Combine(dataDir, "captures", ".ultralytics");
                string matplotlibConfigDir = Path.Combine(dataDir, "captures", ".matplotlib");
                string runtimePython = Path.Combine(currentDir, "runtime", "python.exe");
                string runtimePythonw = Path.Combine(currentDir, "runtime", "pythonw.exe");
                string mainScript = Path.Combine(currentDir, "main.py");
                string startupLog = Path.Combine(logsDir, "startup_error.log");

                Directory.CreateDirectory(dataDir);
                Directory.CreateDirectory(Path.Combine(dataDir, "captures"));
                Directory.CreateDirectory(logsDir);
                Directory.CreateDirectory(yoloConfigDir);
                Directory.CreateDirectory(matplotlibConfigDir);

                if (!File.Exists(runtimePython) || !File.Exists(runtimePythonw) || !File.Exists(mainScript))
                {
                    MessageBox.Show(
                        "找不到完整程式執行核心。\n\n請先將 ZIP 完整解壓縮至本機資料夾，再執行 AG-MONITOR.exe。",
                        "AG-MONITOR - 啟動錯誤",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                    return;
                }

                ProcessStartInfo checkInfo = new ProcessStartInfo
                {
                    FileName = runtimePython,
                    Arguments = "-B -c \"import av, cv2, eel, lap, torch, ultralytics\"",
                    WorkingDirectory = currentDir,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                checkInfo.EnvironmentVariables["AG_MONITOR_DATA_DIR"] = dataDir;
                checkInfo.EnvironmentVariables["YOLO_CONFIG_DIR"] = yoloConfigDir;
                checkInfo.EnvironmentVariables["MPLCONFIGDIR"] = matplotlibConfigDir;
                using (Process check = Process.Start(checkInfo))
                {
                    string output = check.StandardOutput.ReadToEnd();
                    string error = check.StandardError.ReadToEnd();
                    check.WaitForExit();
                    if (check.ExitCode != 0)
                    {
                        File.WriteAllText(startupLog, output + error, new UTF8Encoding(true));
                        MessageBox.Show(
                            "可攜執行環境檢查失敗。\n\n請重新下載並完整解壓縮，詳細內容已寫入：\n" + startupLog,
                            "AG-MONITOR - Runtime 錯誤",
                            MessageBoxButtons.OK,
                            MessageBoxIcon.Error
                        );
                        return;
                    }
                }

                if (File.Exists(startupLog))
                {
                    File.Delete(startupLog);
                }

                ProcessStartInfo startInfo = new ProcessStartInfo
                {
                    FileName = runtimePythonw,
                    Arguments = "-B main.py",
                    WorkingDirectory = currentDir,
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                startInfo.EnvironmentVariables["AG_MONITOR_DATA_DIR"] = dataDir;
                startInfo.EnvironmentVariables["YOLO_CONFIG_DIR"] = yoloConfigDir;
                startInfo.EnvironmentVariables["MPLCONFIGDIR"] = matplotlibConfigDir;
                Process.Start(startInfo);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "啟動過程發生錯誤：\n" + ex.Message,
                    "AG-MONITOR - 錯誤",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
        }
    }
}
