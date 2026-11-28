using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Xml;
using EasyGame;

namespace HeadlessLSC
{
    public class HeadlessInstance
    {
        public string Name;
        public string ServiceClass;
        public string LaunchExe;
        public string SessionId;
        public string IP = "127.0.0.1";
        public string CustomInfo = "";
        public int PID;
        public ServiceStatus Status = (ServiceStatus)1;
        public DateTime StartTime = DateTime.Now;
        public Connection Connection;
        public Process Proc;
        public StreamWriter InputStream;
        public List<string> Commands = new List<string>();

        public ServiceInfo ToServiceInfo()
        {
            return new ServiceInfo
            {
                IP = this.IP,
                HostName = "Local",
                ServiceName = this.Name,
                ServiceClass = this.ServiceClass,
                CustomInfo = this.CustomInfo,
                pid = this.PID,
                Status = this.Status,
                StartTime = this.StartTime
            };
        }
    }

    public class HeadlessManager : ServiceBase, IServiceManagerImpl
    {
        private readonly Dictionary<string, HeadlessInstance> _services = new Dictionary<string, HeadlessInstance>();
        private TcpServiceHolder _holder;
        private readonly int _port = 15000;

        public HeadlessManager() : base((ProtocolFlag)0)
        {
        }

        public void StartAndLaunch(string configName)
        {
            _holder = new TcpServiceHolder(this);
            _holder.Start(new ServiceStartInfo
            {
                IpAddress = "127.0.0.1",
                Port = _port
            });
            Console.WriteLine("[HeadlessLSC] Listening on 127.0.0.1:" + _port);

            var doc = new XmlDocument();
            doc.Load("ServiceConfig.xml");

            XmlNode targetCfg = null;
            foreach (XmlNode c in doc.DocumentElement.SelectNodes("Configs/e"))
            {
                if (c.Attributes["Name"] != null && c.Attributes["Name"].Value == configName)
                {
                    targetCfg = c;
                    break;
                }
            }
            if (targetCfg == null)
            {
                Console.WriteLine("[HeadlessLSC] Config not found: " + configName);
                return;
            }

            var toLaunch = new List<HeadlessInstance>();
            foreach (XmlNode instNode in targetCfg.SelectNodes("Instances/e"))
            {
                var svcCls = instNode.Attributes["ServiceClass"].Value.Trim();
                if (svcCls == "StaticResService")
                    continue;
                var svcName = instNode.Attributes["Name"].Value.Trim();
                var inst = new HeadlessInstance
                {
                    Name = svcName,
                    ServiceClass = svcCls,
                    LaunchExe = svcCls + ".exe",
                    SessionId = Guid.NewGuid().ToString(),
                    Status = (ServiceStatus)1
                };
                foreach (XmlNode cmdNode in instNode.SelectNodes("Command"))
                {
                    inst.Commands.Add(cmdNode.InnerText.Trim());
                }
                _services[inst.Name] = inst;
                toLaunch.Add(inst);
            }

            var order = new List<string> { "DataStorageService", "AgentService", "AuthenticationService", "ManagementService", "PokemonGameService" };
            toLaunch = toLaunch.OrderBy(x => { int idx = order.IndexOf(x.ServiceClass); return idx < 0 ? 99 : idx; }).ToList();

            ThreadPool.QueueUserWorkItem(_ =>
            {
                foreach (var inst in toLaunch)
                {
                    try
                    {
                        LaunchChild(inst);
                        Thread.Sleep(2500);
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine("[HeadlessLSC] Failed launching " + inst.ServiceClass + ": " + ex);
                    }
                }
            });

            // Poll /tmp/pkm/flush_save to flush DataStorageService dirty cache to MariaDB before DB backup/shutdown
            ThreadPool.QueueUserWorkItem(_ =>
            {
                string flag = "/tmp/pkm/flush_save";
                while (true)
                {
                    try
                    {
                        if (File.Exists(flag))
                        {
                            try { File.Delete(flag); } catch { }
                            foreach (var s in _services.Values)
                            {
                                if (s.ServiceClass == "DataStorageService" && s.InputStream != null)
                                {
                                    try
                                    {
                                        s.InputStream.WriteLine("save");
                                        s.InputStream.Flush();
                                        Console.WriteLine("[HeadlessLSC] Sent 'save' flush to DataStorageService");
                                    }
                                    catch { }
                                }
                            }
                        }
                    }
                    catch { }
                    Thread.Sleep(1000);
                }
            });
        }

        private void LaunchChild(HeadlessInstance inst)
        {
            string workDir = Path.Combine(Directory.GetCurrentDirectory(), inst.ServiceClass);
            string logPath = "/tmp/" + inst.ServiceClass + ".log";
            if (Environment.OSVersion.Platform == PlatformID.Win32NT)
            {
                logPath = Path.Combine(Path.GetTempPath(), inst.ServiceClass + ".log");
            }

            var psi = new ProcessStartInfo();
            if (Environment.OSVersion.Platform == PlatformID.Win32NT)
            {
                psi.FileName = Path.Combine(workDir, inst.LaunchExe);
                psi.Arguments = "";
            }
            else
            {
                psi.FileName = "/usr/bin/mono";
                psi.Arguments = "\"" + inst.LaunchExe + "\"";
            }
            psi.WorkingDirectory = workDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardInput = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;

            var proc = new Process { StartInfo = psi };
            var logWriter = TextWriter.Synchronized(new StreamWriter(logPath, false, System.Text.Encoding.UTF8) { AutoFlush = true });
            proc.OutputDataReceived += (s, e) => { if (e.Data != null) logWriter.WriteLine(e.Data); };
            proc.ErrorDataReceived += (s, e) => { if (e.Data != null) logWriter.WriteLine(e.Data); };

            proc.Start();
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();

            inst.Proc = proc;
            inst.PID = proc.Id;
            inst.StartTime = DateTime.Now;
            inst.InputStream = proc.StandardInput;
            inst.InputStream.AutoFlush = true;

            string connCmd = string.Format("ConnectServiceManager 127.0.0.1 {0} {1}", _port, inst.SessionId);
            Console.WriteLine("[HeadlessLSC] Spawned " + inst.ServiceClass + " (PID " + inst.PID + ") -> " + connCmd);

            if (inst.Commands.Contains("{ConnectServiceManager}"))
            {
                foreach (var c in inst.Commands)
                {
                    inst.InputStream.WriteLine(c == "{ConnectServiceManager}" ? connCmd : c);
                }
            }
            else
            {
                inst.InputStream.WriteLine(connCmd);
                foreach (var c in inst.Commands)
                {
                    inst.InputStream.WriteLine(c);
                }
            }
        }

        public override void OnConnection(Session client)
        {
            Console.WriteLine("[HeadlessLSC] OnConnection from " + (client.Endpoint != null ? client.Endpoint.ToString() : "unknown"));
        }

        public override void OnDisconnection(Session client)
        {
            var inst = client.UserData as HeadlessInstance;
            if (inst != null)
            {
                Console.WriteLine("[HeadlessLSC] OnDisconnection: " + inst.Name);
            }
        }

        string IServiceManagerImpl.RegisterService(Session session, string sessionId, MetricSampleDescData[] metricDescription)
        {
            var inst = _services.Values.FirstOrDefault(s => s.SessionId == sessionId);
            if (inst == null)
            {
                Console.WriteLine("[HeadlessLSC] Unknown sessionId: " + sessionId);
                throw new ServiceException(-4, "invalid sessionId");
            }
            inst.IP = session.Endpoint != null ? session.Endpoint.Address.ToString() : "127.0.0.1";
            inst.Connection = session.Connection;
            session.UserData = inst;
            inst.StartTime = DateTime.Now;
            Console.WriteLine("[HeadlessLSC] Registered service: " + inst.Name + " (" + inst.ServiceClass + ")");
            return inst.Name;
        }

        void IServiceManagerImpl.UpdateStatus(Session session, ServiceStatus status)
        {
            var inst = session.UserData as HeadlessInstance;
            if (inst != null)
            {
                inst.Status = status;
                Console.WriteLine("[HeadlessLSC] Status " + inst.Name + " -> " + status);
            }
        }

        void IServiceManagerImpl.Pong(Session session, int tick) { }
        void IServiceManagerImpl.ConsoleLog(Session session, LogLevel level, DateTime logTime, string message) { }
        void IServiceManagerImpl.UploadMetricData(Session session, DateTime timestamp, MetricSampleData[] dataSet) { }

        void IServiceManagerImpl.UpdateCustumInfo(Session session, string info)
        {
            var inst = session.UserData as HeadlessInstance;
            if (inst != null)
            {
                inst.CustomInfo = info ?? "";
                Console.WriteLine("[HeadlessLSC] CustomInfo " + inst.Name + " -> " + inst.CustomInfo);
            }
        }

        ServiceInfo IServiceManagerImpl.GetServiceInfo(Session session, string serviceName)
        {
            HeadlessInstance inst;
            if (_services.TryGetValue(serviceName, out inst))
            {
                return inst.ToServiceInfo();
            }
            return null;
        }

        ServiceInfo[] IServiceManagerImpl.GetServicesInfo(Session session, string prefix)
        {
            return _services.Values
                .Where(s => string.IsNullOrEmpty(prefix) || s.Name.StartsWith(prefix))
                .Select(s => s.ToServiceInfo())
                .ToArray();
        }

        void IServiceManagerImpl.Broadcast(Session session, string subject, string body)
        {
            var sender = session.UserData as HeadlessInstance;
            if (sender == null) return;
            foreach (var s in _services.Values)
            {
                if (s != sender && s.Connection != null)
                {
                    try
                    {
                        IServiceManagerEventProxy.OnBroadcastMessage(s.Connection, sender.Name, subject, body);
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine("[HeadlessLSC] Broadcast err to " + s.Name + ": " + ex.Message);
                    }
                }
            }
        }
    }

    public static class Program
    {
        public static void Main(string[] args)
        {
            string cfg = args.Length > 0 ? args[0] : "Pokemon";
            Thread.CurrentThread.Name = "Main";
            var context = new ServiceContext();
            var mgr = new HeadlessManager();
            mgr.StartAndLaunch(cfg);
            context.Run();
        }
    }
}
