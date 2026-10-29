// 固定继电器接口：构建时编译为 x86，仅显式 set 请求改变触点。
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

internal static class RelayProbe {
    [StructLayout(LayoutKind.Sequential)] private struct Info {
        public IntPtr serial; public IntPtr path; public int channels; public IntPtr next;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern IntPtr LoadLibraryEx(string path, IntPtr reserved, uint flags);
    [DllImport("kernel32.dll")] private static extern bool FreeLibrary(IntPtr library);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int usb_relay_init();
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int usb_relay_exit();
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern IntPtr usb_relay_device_enumerate();
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern void usb_relay_device_free_enumerate(IntPtr head);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Ansi)]
    private static extern IntPtr usb_relay_device_open_with_serial_number([MarshalAs(UnmanagedType.LPStr)] string serial, uint length);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern void usb_relay_device_close(IntPtr handle);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int usb_relay_device_get_status(IntPtr handle, out uint status);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int usb_relay_device_open_one_relay_channel(IntPtr handle, int channel);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int usb_relay_device_close_one_relay_channel(IntPtr handle, int channel);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int usb_relay_device_open_all_relay_channel(IntPtr handle);
    [DllImport("usb_relay_device.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int usb_relay_device_close_all_relay_channel(IntPtr handle);

    // 每个原生调用前后立即写 stderr，辅助进程异常退出时保留最后到达的边界。
    private static void Mark(string stage) {
        string line=DateTime.UtcNow.ToString("o",CultureInfo.InvariantCulture)+" relay-stage:"+stage;
        Console.Error.WriteLine(line); Console.Error.Flush();
        string log=Environment.GetEnvironmentVariable("ADB_RELAY_STAGE_LOG");
        if(!String.IsNullOrEmpty(log)) {
            try { File.AppendAllText(log,line+Environment.NewLine,new UTF8Encoding(false)); }
            catch(Exception) { /* 日志写入失败不能改变硬件调用结果。 */ }
        }
    }
    private static void Done(string stage) { Console.WriteLine("{\"success\":true,\"stage\":"+Quote(stage)+"}"); }
    private static string Quote(string value) {
        var result=new StringBuilder("\"");
        foreach(char ch in value) {
            if(ch=='"' || ch=='\\') result.Append('\\').Append(ch);
            else if(ch<32) result.Append("\\u").Append(((int)ch).ToString("x4",CultureInfo.InvariantCulture));
            else result.Append(ch);
        }
        return result.Append('"').ToString();
    }
    private static void CheckSerial(string serial) {
        if(serial==null || !Regex.IsMatch(serial,@"\A[A-Za-z0-9]{1,64}\z"))
            throw new InvalidOperationException("设备序列号格式异常");
    }
    public static int Main(string[] args) {
        Console.OutputEncoding=new UTF8Encoding(false);
        IntPtr library=IntPtr.Zero;
        try {
            if(args.Length<2 || Array.IndexOf(new[]{"check","load","init","scan","status","set"},args[0])<0 ||
               args.Length!=(args[0]=="status"?3:args[0]=="set"?5:2))
                throw new ArgumentException("支持 check/load/init/scan <工具目录>、status <工具目录> <序列号>、set <工具目录> <序列号> <通道> <open/close>");
            if(args[0]=="status") CheckSerial(args[2]);
            if(args[0]=="set") {
                CheckSerial(args[2]);
                int channel; if(!Int32.TryParse(args[3],out channel) || (channel!=255 && (channel<1 || channel>8)))
                    throw new ArgumentException("通道必须为1到8或255");
                if(args[4]!="open" && args[4]!="close") throw new ArgumentException("操作必须为open或close");
            }
            Mark("request-"+args[0]);
            string path=Path.Combine(Path.GetFullPath(args[1]),"usb_relay_device.dll");
            if(!File.Exists(path)) throw new FileNotFoundException("工具目录缺少 usb_relay_device.dll");
            Mark("files-checked");
            if(args[0]=="check") { Done("check"); return 0; }
            // 只从明确指定的 DLL 目录及系统目录解析依赖，不修改进程全局搜索路径。
            Mark("before-load"); library=LoadLibraryEx(path,IntPtr.Zero,0x00000100|0x00000800);
            if(library==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(),"加载 32 位继电器 DLL 失败");
            Mark("after-load");
            if(args[0]=="load") { Done("load"); return 0; }
            Mark("before-init");
            if(usb_relay_init()!=0) throw new InvalidOperationException("继电器库初始化失败");
            Mark("after-init");
            try {
                if(args[0]=="init") { Done("init"); return 0; }
                if(args[0]=="scan") {
                    IntPtr head=IntPtr.Zero;
                    try {
                        Mark("before-enumerate"); head=usb_relay_device_enumerate(); Mark("after-enumerate");
                        var devices=new List<string>(); var seen=new HashSet<IntPtr>();
                        for(IntPtr p=head;p!=IntPtr.Zero;) {
                            if(!seen.Add(p) || seen.Count>128) throw new InvalidOperationException("设备枚举数据异常");
                            Info info=(Info)Marshal.PtrToStructure(p,typeof(Info));
                            string serial=Marshal.PtrToStringAnsi(info.serial); CheckSerial(serial);
                            if(info.channels<1 || info.channels>8) throw new InvalidOperationException("设备通道数量异常");
                            devices.Add("{\"serial\":"+Quote(serial)+",\"channels\":"+info.channels.ToString(CultureInfo.InvariantCulture)+"}");
                            p=info.next;
                        }
                        Console.WriteLine("{\"success\":true,\"devices\":["+String.Join(",",devices.ToArray())+"]}");
                    } finally {if(head!=IntPtr.Zero) { Mark("before-free-enumerate"); usb_relay_device_free_enumerate(head); Mark("after-free-enumerate"); }}
                } else {
                    IntPtr handle=IntPtr.Zero;
                    try {
                        string serial=args[2];
                        Mark("before-open"); handle=usb_relay_device_open_with_serial_number(serial,(uint)serial.Length); Mark("after-open");
                        if(handle==IntPtr.Zero) throw new InvalidOperationException("无法打开设备，请检查连接及工具占用");
                        if(args[0]=="status") {
                            Mark("before-status"); uint mask; if(usb_relay_device_get_status(handle,out mask)!=0) throw new InvalidOperationException("读取通道状态失败"); Mark("after-status");
                            Console.WriteLine("{\"success\":true,\"mask\":"+mask.ToString(CultureInfo.InvariantCulture)+"}");
                        } else {
                            int channel=Int32.Parse(args[3],CultureInfo.InvariantCulture); bool on=args[4]=="open";
                            Mark("before-set"); int result;
                            if(channel==255) result=on?usb_relay_device_open_all_relay_channel(handle):usb_relay_device_close_all_relay_channel(handle);
                            else result=on?usb_relay_device_open_one_relay_channel(handle,channel):usb_relay_device_close_one_relay_channel(handle,channel);
                            Mark("after-set"); if(result!=0) throw new InvalidOperationException("继电器控制接口返回失败");
                            // 控制已成功，回读失败只能标记状态未知，不得误报控制失败。
                            try {
                                Mark("before-status"); uint mask;
                                if(usb_relay_device_get_status(handle,out mask)!=0) throw new InvalidOperationException("读取通道状态失败");
                                Mark("after-status");
                                Console.WriteLine("{\"success\":true,\"mask\":"+mask.ToString(CultureInfo.InvariantCulture)+"}");
                            } catch(Exception error) {
                                Console.WriteLine("{\"success\":true,\"readError\":"+Quote(error.GetBaseException().Message)+"}");
                            }
                        }
                    } finally {if(handle!=IntPtr.Zero) { Mark("before-close"); usb_relay_device_close(handle); Mark("after-close"); }}
                }
            } finally {Mark("before-exit"); usb_relay_exit(); Mark("after-exit");}
            return 0;
        } catch(Exception error) {
            Console.WriteLine("{\"success\":false,\"output\":"+Quote(error.GetBaseException().Message)+"}");
            return 1;
        } finally {if(library!=IntPtr.Zero) { Mark("before-unload"); FreeLibrary(library); Mark("after-unload"); }}
    }
}
