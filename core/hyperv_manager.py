import os
import subprocess

from core.powershell import run_powershell, ElevatedSession


class HyperVManager:
    def __init__(self, vm_name, switch_name, nat_name, subnet):
        self.vm_name = vm_name
        self.switch_name = switch_name
        self.nat_name = nat_name
        self.subnet = subnet
        self._elevated = None

    def _admin(self):
        """获取或创建提权会话（首次使用时弹 UAC）"""
        if self._elevated is None:
            self._elevated = ElevatedSession()
        return self._elevated

    def is_hyperv_enabled(self):
        result = run_powershell(
            "(Get-Service vmms -ErrorAction SilentlyContinue).Status"
        )
        return result.ok and "Running" in result.stdout

    def get_windows_edition(self):
        result = run_powershell(
            "(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion').EditionID"
        )
        return result.stdout.strip() if result.ok else ""

    def is_home_edition(self):
        edition = self.get_windows_edition().lower()
        return edition in {"core", "coren", "corecountryspecific", "coreSingleLanguage".lower()}

    def is_hyperv_management_available(self):
        result = run_powershell(
            "if (Get-Command Get-VM -ErrorAction SilentlyContinue) { 'yes' } else { 'no' }"
        )
        return result.ok and result.stdout == "yes"

    def can_force_enable_on_home(self):
        packages_dir = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "servicing",
            "Packages",
        )
        if not os.path.isdir(packages_dir):
            return False
        try:
            return any("Hyper-V" in name and name.endswith(".mum") for name in os.listdir(packages_dir))
        except OSError:
            return False

    def vm_exists(self):
        result = self._admin().run(
            f"if (Get-VM -Name '{self.vm_name}' -ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}"
        )
        return result.ok and "yes" in result.stdout

    def ensure_switch(self):
        command = (
            f"$switch = Get-VMSwitch -Name '{self.switch_name}' -ErrorAction SilentlyContinue; "
            f"if (-not $switch) {{ New-VMSwitch -SwitchName '{self.switch_name}' -SwitchType Internal | Out-Null }}"
        )
        result = self._admin().run(command)
        if not result.ok:
            self._show_error_window("创建虚拟交换机", command, result)
        return result.ok

    def ensure_nat(self, gateway_ip):
        prefix = f"{gateway_ip}/{self.subnet.split('/')[1]}"
        command = (
            f"$adapter = Get-NetAdapter | Where-Object {{$_.Name -Like '*{self.switch_name}*'}} | Select-Object -First 1; "
            f"if ($adapter) {{ "
            f"$ip = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            f"Where-Object {{$_.IPAddress -eq '{gateway_ip}'}}; "
            f"if (-not $ip) {{ New-NetIPAddress -IPAddress '{gateway_ip}' -PrefixLength {self.subnet.split('/')[1]} "
            f"-InterfaceIndex $adapter.ifIndex | Out-Null }} "
            f"}}; "
            f"if (-not (Get-NetNat -Name '{self.nat_name}' -ErrorAction SilentlyContinue)) {{ "
            f"New-NetNat -Name '{self.nat_name}' -InternalIPInterfaceAddressPrefix '{prefix}' | Out-Null }}"
        )
        result = self._admin().run(command)
        if not result.ok:
            self._show_error_window("配置 NAT", command, result)
        return result.ok

    def create_vm(self, vm_dir, base_vhdx):
        os.makedirs(vm_dir, exist_ok=True)
        vm_vhdx = os.path.join(vm_dir, f"{self.vm_name}.vhdx").replace("\\", "/")
        base_vhdx = base_vhdx.replace("\\", "/")
        # 支持传入 .vhd，自动转换为 .vhdx
        if base_vhdx.endswith(".vhd") and not base_vhdx.endswith(".vhdx"):
            convert_step = (
                f"if (-not (Test-Path '{vm_vhdx}')) {{ "
                f"Convert-VHD -Path '{base_vhdx}' -DestinationPath '{vm_vhdx}' -VHDType Dynamic }}; "
            )
        else:
            convert_step = (
                f"if (-not (Test-Path '{vm_vhdx}')) {{ Copy-Item -Path '{base_vhdx}' -Destination '{vm_vhdx}' -Force }}; "
            )
        command = (
            convert_step
            + f"if (-not (Get-VM -Name '{self.vm_name}' -ErrorAction SilentlyContinue)) {{ "
            f"New-VM -Name '{self.vm_name}' -MemoryStartupBytes 4GB -Generation 2 -VHDPath '{vm_vhdx}' "
            f"-SwitchName '{self.switch_name}' | Out-Null; "
            f"Set-VMProcessor -VMName '{self.vm_name}' -Count 2 | Out-Null; "
            f"Set-VMFirmware -VMName '{self.vm_name}' -EnableSecureBoot Off | Out-Null }}"
        )
        result = self._admin().run(command, timeout=300)
        if not result.ok:
            self._show_error_window("创建虚拟机", command, result)
        return result.ok, vm_vhdx

    def get_vm_mac_address(self):
        result = self._admin().run(
            f"(Get-VMNetworkAdapter -VMName '{self.vm_name}' | Select-Object -First 1 -ExpandProperty MacAddress)"
        )
        return result.stdout.strip() if result.ok else ""

    def attach_seed_disk(self, seed_disk_path):
        seed_disk_path = seed_disk_path.replace("\\", "/")
        command = (
            f"$dvd = Get-VMDvdDrive -VMName '{self.vm_name}' -ErrorAction SilentlyContinue | "
            f"Where-Object {{$_.Path -eq '{seed_disk_path}'}}; "
            f"if (-not $dvd) {{ Add-VMDvdDrive -VMName '{self.vm_name}' -Path '{seed_disk_path}' }}"
        )
        return self._admin().run(command).ok

    def start_vm(self):
        return self._admin().run(
            f"$vm = Get-VM -Name '{self.vm_name}' -ErrorAction SilentlyContinue; "
            f"if ($vm -and $vm.State -ne 'Running') {{ Start-VM -Name '{self.vm_name}' | Out-Null }}",
        ).ok

    def stop_vm(self):
        return self._admin().run(
            f"Stop-VM -Name '{self.vm_name}' -Force -TurnOff",
        ).ok

    def remove_vm(self):
        return self._admin().run(
            f"if (Get-VM -Name '{self.vm_name}' -ErrorAction SilentlyContinue) "
            f"{{ Stop-VM -Name '{self.vm_name}' -Force -TurnOff -ErrorAction SilentlyContinue; "
            f"Remove-VM -Name '{self.vm_name}' -Force }}",
        ).ok

    def stop_elevated(self):
        """停止提权会话"""
        if self._elevated:
            self._elevated.stop()
            self._elevated = None

    def _show_error_window(self, action, command, result):
        """弹出一个 PowerShell 窗口展示失败的命令和详细错误信息"""
        msg = (
            f"Write-Host '=== Hyper-V 操作失败: {action} ===' -ForegroundColor Red; "
            f"Write-Host ''; "
            f"Write-Host '--- 退出码 ---' -ForegroundColor Yellow; "
            f"Write-Host '{result.returncode}'; "
            f"Write-Host ''; "
            f"Write-Host '--- STDOUT ---' -ForegroundColor Yellow; "
            f"Write-Host @'\n{result.stdout}\n'@; "
            f"Write-Host ''; "
            f"Write-Host '--- STDERR ---' -ForegroundColor Yellow; "
            f"Write-Host @'\n{result.stderr}\n'@; "
            f"Write-Host ''; "
            f"Write-Host '--- 执行的命令 ---' -ForegroundColor Yellow; "
            f"Write-Host @'\n{command}\n'@; "
            f"Write-Host ''; "
            f"Write-Host '按任意键关闭...' -ForegroundColor Cyan; "
            f"$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NoExit", "-Command", msg],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )

    def create_seed_disk(self, seed_disk_path, source_dir):
        """用 IMAPI2 创建 cloud-init NoCloud ISO（cidata 卷标）"""
        seed_disk_path = seed_disk_path.replace("\\", "/")
        source_dir = source_dir.replace("\\", "/")
        os.makedirs(os.path.dirname(seed_disk_path), exist_ok=True)
        command = (
            f"if (Test-Path '{seed_disk_path}') {{ Remove-Item '{seed_disk_path}' -Force }}; "
            f"$fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage; "
            f"$fsi.FileSystemsToCreate = 3; "
            f"$fsi.VolumeName = 'cidata'; "
            f"$fsi.Root.AddTree('{source_dir}', $false); "
            f"$result = $fsi.CreateResultImage(); "
            f"$stream = $result.ImageStream; "
            f"$stream.Seek(0, 0) | Out-Null; "
            f"$fs = [System.IO.File]::Create('{seed_disk_path}'); "
            f"$buf = New-Object byte[] 2048; "
            f"while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {{ $fs.Write($buf, 0, $n) }}; "
            f"$fs.Close()"
        )
        return run_powershell(command, timeout=180).ok

    def ensure_portproxy(self, listen_port, connect_address, connect_port):
        command = (
            f"netsh interface portproxy delete v4tov4 listenport={listen_port} listenaddress=127.0.0.1 | Out-Null; "
            f"netsh interface portproxy add v4tov4 listenport={listen_port} listenaddress=127.0.0.1 "
            f"connectport={connect_port} connectaddress={connect_address}"
        )
        return self._admin().run(command).ok
