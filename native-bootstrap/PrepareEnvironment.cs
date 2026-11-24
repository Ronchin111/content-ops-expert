using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

[DataContract] public sealed class EntryInput {
    [DataMember(Order=1)] public string Platform;
    [DataMember(Order=2)] public string ToolRoot;
    [DataMember(Order=3)] public string PythonPath;
    [DataMember(Order=4)] public string TrustedPwsh;
    [DataMember(Order=5)] public string ArchivePath;
    [DataMember(Order=6)] public string Target;
}
[DataContract] public sealed class EntryPlan {
    [DataMember(Order=1)] public string Version="PAY-S3-06-candidate1";
    [DataMember(Order=2)] public EntryInput Input;
    [DataMember(Order=3)] public string State;
    [DataMember(Order=4)] public int WindowsBuild;
    [DataMember(Order=5)] public string Architecture;
    [DataMember(Order=6)] public string ProgramSha256;
    [DataMember(Order=7)] public string CheckerPath;
    [DataMember(Order=8)] public string CheckerSha256;
    [DataMember(Order=9)] public string PwshPath;
    [DataMember(Order=10)] public string PwshSha256;
    [DataMember(Order=11)] public string ArchiveSha256;
    [DataMember(Order=12)] public string ArchiveUrl=Entry.ArchiveUrl;
    [DataMember(Order=13)] public long ArchiveBytes=Entry.ArchiveBytes;
    [DataMember(Order=14)] public bool CreateParent;
    [DataMember(Order=15)] public List<string> Blockers=new List<string>();
    [DataMember(Order=16)] public List<string> Actions=new List<string>();
    [DataMember(Order=17)] public bool SelectedPythonExists;
    [DataMember(Order=18)] public string SelectedPythonSha256;
}
[DataContract] public sealed class EntryReceipt {
    [DataMember(Order=1)] public string Version="PAY-S3-06-candidate1";
    [DataMember(Order=2)] public string State="entry_incomplete";
    [DataMember(Order=3)] public string Stage="prepare";
    [DataMember(Order=4)] public string ApprovedPlanSha256;
    [DataMember(Order=5)] public bool ExtractionAttempted;
    [DataMember(Order=6)] public bool ChildExecutionAttempted;
    [DataMember(Order=7)] public int? ChildExitCode;
    [DataMember(Order=8)] public bool ChildMayStillRun;
    [DataMember(Order=9)] public string ErrorCode;
    [DataMember(Order=10)] public bool ExportStarted=false;
    [DataMember(Order=11)] public bool ConfigurationChanged=false;
}
[DataContract] public sealed class CheckerEnvelope {
    [DataMember(Name="schema_version")] public int Schema;
    [DataMember(Name="state")] public string State;
    [DataMember(Name="export_started")] public bool ExportStarted;
}
public static class Entry {
    public const string ArchiveUrl="https://github.com/PowerShell/PowerShell/releases/download/v7.4.20/PowerShell-7.4.20-win-x64.zip";
    public const long ArchiveBytes=111969701;
    public const string ArchiveHash="fb88cd3731847006b157978d6c0a426390d346696d91d1ed85b5e5eb88cb2d40";
    public const string CheckerHash="c29864fb4fae58c817b42964b4fbc176d290fa8d11240b572ee4221923eff24b";
    static readonly string ProgramPath=Assembly.GetExecutingAssembly().Location;
    static readonly string PackageRoot=Path.GetDirectoryName(ProgramPath);
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct OsInfo {
        public uint Size,Major,Minor,Build,Platform;
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=128)] public string ServicePack;
        public ushort ServicePackMajor,ServicePackMinor,SuiteMask;
        public byte ProductType,Reserved;
    }
    [StructLayout(LayoutKind.Sequential)] struct SystemInfo {
        public ushort Architecture,Reserved; public uint PageSize;
        public IntPtr Minimum,Maximum,Mask; public uint Processors,Type,Granularity;
        public ushort Level,Revision;
    }
    [DllImport("ntdll.dll",CharSet=CharSet.Unicode)] static extern int RtlGetVersion(ref OsInfo info);
    [DllImport("kernel32.dll")] static extern void GetNativeSystemInfo(out SystemInfo info);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateDirectory(string path,IntPtr attributes);

    public static string Local(string value) {
        if (String.IsNullOrEmpty(value)||!Regex.IsMatch(value,@"\A[A-Za-z]:[\\/]")) throw new Exception("absolute_local_path_required");
        if (Regex.IsMatch(value,"[\x00-\x1f\"]")) throw new Exception("path_characters_invalid");
        string full=Path.GetFullPath(value).TrimEnd('\\');
        if (full.Length<4) throw new Exception("root_directory_not_allowed");
        DriveInfo drive=new DriveInfo(Path.GetPathRoot(full));
        if (drive.DriveType!=DriveType.Fixed&&drive.DriveType!=DriveType.Removable) throw new Exception("local_drive_required");
        string part=Path.GetPathRoot(full);
        foreach(string item in full.Substring(3).Split('\\')) {
            part=Path.Combine(part,item);
            if (!File.Exists(part)&&!Directory.Exists(part)) break;
            if ((File.GetAttributes(part)&FileAttributes.ReparsePoint)!=0) throw new Exception("reparse_path_rejected");
        }
        return full;
    }
    public static string Hash(Stream stream) {
        using(SHA256 algorithm=SHA256.Create()) return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-","").ToLowerInvariant();
    }
    public static string HashFile(string path) { using(FileStream file=File.OpenRead(Local(path))) return Hash(file); }
    public static byte[] Json<T>(T value) { using(MemoryStream stream=new MemoryStream()) { new DataContractJsonSerializer(typeof(T)).WriteObject(stream,value); return stream.ToArray(); } }
    public static T Parse<T>(byte[] bytes) { using(MemoryStream stream=new MemoryStream(bytes)) return (T)new DataContractJsonSerializer(typeof(T)).ReadObject(stream); }
    public static void NewFile(string path,byte[] data) { using(FileStream stream=new FileStream(Local(path),FileMode.CreateNew,FileAccess.Write,FileShare.None)) stream.Write(data,0,data.Length); }
    public static void NewDirectory(string path) { path=Local(path); if(!CreateDirectory(path,IntPtr.Zero)) throw new Exception("new_directory_not_created"); }

    public static void ValidateEntries(ZipArchive zip) {
        HashSet<string> names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        HashSet<string> files=new HashSet<string>(StringComparer.OrdinalIgnoreCase); long total=0;
        if(zip.Entries.Count==0||zip.Entries.Count>12000) throw new Exception("zip_entry_limit");
        foreach(ZipArchiveEntry entry in zip.Entries) {
            string name=entry.FullName.TrimEnd('/');
            if(name.Length==0||name.StartsWith("/")||Regex.IsMatch(name,@"[\\\x00-\x1f<>:""|?*]")||!names.Add(name)) throw new Exception("zip_path_or_duplicate_invalid");
            foreach(string part in name.Split('/')) if(part.Length==0||part=="."||part==".."||Regex.IsMatch(part,@"[. ]\z")||Regex.IsMatch(part,@"\A(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|\z)",RegexOptions.IgnoreCase)) throw new Exception("zip_path_invalid");
            if(((entry.ExternalAttributes>>16)&61440)==40960||(entry.ExternalAttributes&1024)!=0) throw new Exception("zip_link_rejected");
            if(!entry.FullName.EndsWith("/")) files.Add(name);
            total=checked(total+entry.Length);
            if(total>1073741824||entry.Length>268435456) throw new Exception("zip_expansion_limit");
        }
        foreach(string name in names) { string[] parts=name.Split('/'); string parent=""; for(int i=0;i<parts.Length-1;i++) { parent=parent.Length==0?parts[i]:parent+"/"+parts[i]; if(files.Contains(parent)) throw new Exception("zip_parent_is_file"); } }
        foreach(string required in new string[]{"pwsh.exe","pwsh.dll","pwsh.deps.json","pwsh.runtimeconfig.json","System.Management.Automation.dll"}) if(!files.Contains(required)) throw new Exception("zip_runtime_file_missing");
    }
    static void Archive(string source,string destination) {
        using(FileStream stream=new FileStream(Local(source),FileMode.Open,FileAccess.Read,FileShare.Read)) {
            if(stream.Length!=ArchiveBytes||Hash(stream)!=ArchiveHash) throw new Exception("official_archive_mismatch");
            stream.Position=0;
            using(ZipArchive zip=new ZipArchive(stream,ZipArchiveMode.Read,true)) {
                ValidateEntries(zip); if(destination==null) return;
                foreach(ZipArchiveEntry entry in zip.Entries) {
                    string path=Local(Path.Combine(destination,entry.FullName.TrimEnd('/').Replace('/','\\')));
                    if(!path.StartsWith(destination+"\\",StringComparison.OrdinalIgnoreCase)) throw new Exception("zip_escape_rejected");
                    string parent=entry.FullName.EndsWith("/")?path:Path.GetDirectoryName(path);
                    Directory.CreateDirectory(Local(parent));
                    if(entry.FullName.EndsWith("/")) continue;
                    using(Stream input=entry.Open()) using(FileStream output=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None)) input.CopyTo(output);
                }
            }
        }
    }
    public static EntryPlan MakePlan(EntryInput input) {
        EntryPlan p=new EntryPlan(); p.Input=input;
        if(input.Platform!="douyin"&&input.Platform!="wechat"&&input.Platform!="xhs") throw new Exception("platform_required");
        input.ToolRoot=Local(input.ToolRoot); input.Target=Local(input.Target);
        if(!String.IsNullOrEmpty(input.PythonPath)) { input.PythonPath=Local(input.PythonPath); p.SelectedPythonExists=File.Exists(input.PythonPath); if(p.SelectedPythonExists) p.SelectedPythonSha256=HashFile(input.PythonPath); }
        OsInfo os=new OsInfo(); os.Size=(uint)Marshal.SizeOf(typeof(OsInfo)); if(RtlGetVersion(ref os)!=0) throw new Exception("os_query_failed");
        SystemInfo machine; GetNativeSystemInfo(out machine); p.WindowsBuild=(int)os.Build; p.Architecture=machine.Architecture==9?"x64":"unsupported";
        if(os.Major<10||os.Build<22000||os.ProductType!=1||machine.Architecture!=9) p.Blockers.Add("candidate_supports_windows11_x64_workstation_only");
        p.ProgramSha256=HashFile(ProgramPath); p.CheckerPath=Local(Path.Combine(PackageRoot,"scripts","check_environment.ps1"));
        if(!File.Exists(p.CheckerPath)) p.Blockers.Add("checker_missing"); else { p.CheckerSha256=HashFile(p.CheckerPath); if(p.CheckerSha256!=CheckerHash) p.Blockers.Add("checker_version_mismatch"); }
        if(File.Exists(input.Target)||Directory.Exists(input.Target)) p.Blockers.Add("new_target_required");
        string parent=Path.GetDirectoryName(input.Target); p.CreateParent=!Directory.Exists(parent)&&String.Equals(parent,Path.Combine(PackageRoot,"runtime"),StringComparison.OrdinalIgnoreCase);
        if(!Directory.Exists(parent)&&!p.CreateParent) p.Blockers.Add("target_parent_missing");
        if(input.Target.StartsWith(input.ToolRoot.TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase)||String.Equals(input.Target,input.ToolRoot,StringComparison.OrdinalIgnoreCase)) p.Blockers.Add("target_inside_tool_rejected");
        if(!String.IsNullOrEmpty(input.TrustedPwsh)) {
            input.TrustedPwsh=Local(input.TrustedPwsh); p.PwshPath=input.TrustedPwsh;
            if(!File.Exists(p.PwshPath)||!String.Equals(Path.GetFileName(p.PwshPath),"pwsh.exe",StringComparison.OrdinalIgnoreCase)) p.Blockers.Add("explicit_trusted_pwsh_invalid");
            else { string raw=FileVersionInfo.GetVersionInfo(p.PwshPath).ProductVersion; Match match=Regex.Match(raw??"",@"\A(\d+\.\d+\.\d+)"); Version v;
                if(!match.Success||!Version.TryParse(match.Value,out v)||v<new Version(7,4,0)) p.Blockers.Add("pwsh_74_required"); p.PwshSha256=HashFile(p.PwshPath); }
            if(input.Target.StartsWith(Path.GetDirectoryName(input.TrustedPwsh).TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase)) p.Blockers.Add("target_inside_existing_pwsh_rejected");
        } else {
            input.ArchivePath=Local(input.ArchivePath); p.PwshPath=Path.Combine(input.Target,"powershell","pwsh.exe");
            if(!File.Exists(input.ArchivePath)) p.Blockers.Add("stage_official_powershell_zip_first");
            else { Archive(input.ArchivePath,null); p.ArchiveSha256=ArchiveHash; }
        }
        p.State=p.Blockers.Count>0?"preparation_required":"awaiting_specific_approval";
        if(p.Blockers.Count==0) { if(String.IsNullOrEmpty(input.TrustedPwsh)) p.Actions.Add("extract_powershell_zip_to_new_target"); p.Actions.Add("run_fixed_checker_under_normal_execution_policy"); }
        return p;
    }
    static string Quote(string value) { return "\""+value+"\""; }
    public static string ClassifyChecker(byte[] bytes,int exitCode,bool stderrPresent) {
        if(stderrPresent||bytes.Length==0) return "checker_refused_or_incomplete";
        try {
            string text;
            try { text=new UTF8Encoding(false,true).GetString(bytes); }
            catch(DecoderFallbackException) { text=Encoding.GetEncoding(936,EncoderFallback.ExceptionFallback,DecoderFallback.ExceptionFallback).GetString(bytes); }
            CheckerEnvelope result=Parse<CheckerEnvelope>(new UTF8Encoding(false).GetBytes(text));
            if(result.Schema!=1||result.ExportStarted) return "checker_refused_or_incomplete";
            if(exitCode==0&&result.State=="prerequisites_ready") return "prerequisites_checked";
            if(exitCode==1&&result.State=="needs_environment") return "prerequisites_need_attention";
        } catch { }
        return "checker_refused_or_incomplete";
    }
    public static EntryReceipt Execute(string path,string approved) {
        path=Local(path); if(!Regex.IsMatch(approved??"",@"\A[0-9a-fA-F]{64}\z")||HashFile(path)!=approved.ToLowerInvariant()) throw new Exception("approval_hash_mismatch");
        if(new FileInfo(path).Length>65536) throw new Exception("plan_limit"); byte[] bytes=File.ReadAllBytes(path); EntryPlan saved=Parse<EntryPlan>(bytes);
        EntryPlan fresh=MakePlan(saved.Input); if(Encoding.UTF8.GetString(Json(fresh))!=Encoding.UTF8.GetString(bytes)||fresh.State!="awaiting_specific_approval") throw new Exception("plan_stale_or_blocked");
        if(fresh.CreateParent) NewDirectory(Path.GetDirectoryName(fresh.Input.Target)); NewDirectory(fresh.Input.Target);
        EntryReceipt r=new EntryReceipt(); r.ApprovedPlanSha256=approved.ToLowerInvariant();
        try {
            NewFile(Path.Combine(fresh.Input.Target,"approved-plan.json"),bytes);
            if(String.IsNullOrEmpty(fresh.Input.TrustedPwsh)) { r.Stage="extract_powershell"; r.ExtractionAttempted=true; string runtime=Path.Combine(fresh.Input.Target,"powershell"); NewDirectory(runtime); Archive(fresh.Input.ArchivePath,runtime); }
            r.Stage="run_readonly_checker"; r.ChildExecutionAttempted=true;
            ProcessStartInfo start=new ProcessStartInfo(); start.FileName=Local(fresh.PwshPath); start.UseShellExecute=false; start.CreateNoWindow=true; start.RedirectStandardOutput=true; start.RedirectStandardError=true;
            start.Arguments="-NoProfile -File "+Quote(fresh.CheckerPath)+" -Platform "+fresh.Input.Platform+" -ToolRoot "+Quote(fresh.Input.ToolRoot);
            if(!String.IsNullOrEmpty(fresh.Input.PythonPath)) start.Arguments+=" -PythonPath "+Quote(fresh.Input.PythonPath);
            using(Process process=Process.Start(start)) {
                using(MemoryStream stdout=new MemoryStream()) using(MemoryStream stderr=new MemoryStream()) {
                    var outTask=process.StandardOutput.BaseStream.CopyToAsync(stdout); var errTask=process.StandardError.BaseStream.CopyToAsync(stderr);
                    if(!process.WaitForExit(35000)) { r.ChildMayStillRun=true; r.ErrorCode="readonly_check_timeout_no_retry"; return r; }
                    if(!outTask.Wait(1000)||!errTask.Wait(1000)) throw new Exception("check_output_incomplete");
                    r.ChildExitCode=process.ExitCode;
                    if(stdout.Length>262144) throw new Exception("check_output_limit");
                    // Keep raw bytes: never guess a child host's console encoding.
                    NewFile(Path.Combine(fresh.Input.Target,"checker.stdout.bin"),stdout.ToArray());
                    r.State=ClassifyChecker(stdout.ToArray(),process.ExitCode,stderr.Length>0);
                    if(stderr.Length>0) r.ErrorCode="checker_reported_stderr";
                }
            }
        } catch { r.ErrorCode="entry_stage_failed"; }
        finally { NewFile(Path.Combine(fresh.Input.Target,"receipt.json"),Json(r)); }
        return r;
    }
    public static int Main(string[] args) {
        Console.OutputEncoding=new UTF8Encoding(false);
        try {
            Dictionary<string,string> options=new Dictionary<string,string>();
            HashSet<string> allowed=new HashSet<string>(new string[]{"--platform","--tool-root","--python","--trusted-pwsh","--archive","--target","--save-plan","--execute-plan","--approved-sha256"});
            if(args.Length==0||(args.Length==1&&args[0]=="--help")) { Console.WriteLine("PrepareEnvironment --platform douyin|wechat|xhs --tool-root ABSOLUTE_PATH [--trusted-pwsh TRUSTED_EXE | --archive OFFICIAL_ZIP] [--python TRUSTED_EXE] [--target NEW_DIRECTORY] [--save-plan NEW_JSON]\nDefault: plan only; no download/install/script-policy change. Execute only after specific approval: --execute-plan PLAN --approved-sha256 HASH"); return 0; }
            for(int i=0;i<args.Length;i+=2) { if(i+1>=args.Length||!allowed.Contains(args[i])||options.ContainsKey(args[i])) throw new Exception("arguments_invalid"); options.Add(args[i],args[i+1]); }
            if(options.ContainsKey("--execute-plan")) { if(options.Count!=2||!options.ContainsKey("--approved-sha256")) throw new Exception("execution_arguments_invalid"); EntryReceipt receipt=Execute(options["--execute-plan"],options["--approved-sha256"]); Console.WriteLine(Encoding.UTF8.GetString(Json(receipt))); return receipt.State=="prerequisites_checked"?0:1; }
            if(!options.ContainsKey("--platform")||!options.ContainsKey("--tool-root")||options.ContainsKey("--approved-sha256")) throw new Exception("plan_arguments_invalid");
            EntryInput input=new EntryInput(); input.Platform=options["--platform"]; input.ToolRoot=options["--tool-root"];
            input.PythonPath=options.ContainsKey("--python")?options["--python"]:null; input.TrustedPwsh=options.ContainsKey("--trusted-pwsh")?options["--trusted-pwsh"]:null;
            input.ArchivePath=options.ContainsKey("--archive")?options["--archive"]:Path.Combine(PackageRoot,"packages","PowerShell-7.4.20-win-x64.zip");
            input.Target=options.ContainsKey("--target")?options["--target"]:Path.Combine(PackageRoot,"runtime","first-environment-check");
            EntryPlan plan=MakePlan(input); byte[] json=Json(plan); if(options.ContainsKey("--save-plan")) NewFile(options["--save-plan"],json); Console.WriteLine(Encoding.UTF8.GetString(json)); return plan.State=="preparation_required"?1:0;
        } catch { Console.WriteLine("{\"State\":\"entry_incomplete\",\"ErrorCode\":\"input_approval_or_io_failed\",\"ConfigurationChanged\":false,\"ExportStarted\":false}"); return 2; }
    }
}
