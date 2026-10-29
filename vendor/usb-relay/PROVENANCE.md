# USB 继电器上游来源

- 上游：https://github.com/pavel-a/usb-relay-hid
- 源码快照：上游标签 `usb-relay-lib_v2.1` 指向提交 `8553cfb529a67a4375ffa151bba41b4dff7acc1f`；保留 Windows DLL 所需源文件及构建配置。
- 32位 DLL：上游发布版本 `usb-relay-lib_v2.1` 的 `usb-relay-hid_bin-20150330a.zip` 内 `bin-Win32/USB_RELAY_DEVICE.dll`。
- DLL SHA256：`3BE596065C72349CEEBC9B3043FAE7153920E8579647E2C1AAC2DF3C206A3565`。它来自与标签同一上游发布，但没有可复现构建证据证明二进制由标签源码编译。
- 上游 README 声明包含 GPL + commercial 双许可代码；`commandline/hiddata.h` 标明 GPL v2、GPL v3 或商业许可。正式分发前需审查组合软件义务及完整对应源码；目前未取得商业授权，也没有完成法律合规结论。
