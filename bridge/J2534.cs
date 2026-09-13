using System;
using System.Runtime.InteropServices;
using System.Text;

namespace ScanmatikBridge;

/// <summary>
/// Минимальная обвязка над SAE J2534-1 (v04.04) PassThru API.
/// DLL грузится динамически (LoadLibrary/GetProcAddress), т.к. путь к ней
/// приходит во время выполнения (пользователь выбирает драйвер в GUI).
/// Реализованы функции, нужные для чтения (логирования) и отправки кадров
/// CAN/J1939: Open/Close/Connect/Disconnect/StartMsgFilter/ReadMsgs/
/// WriteMsgs/GetLastError.
/// </summary>
internal static class NativeLibrary
{
    public const uint LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008;

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi, BestFitMapping = false)]
    public static extern IntPtr LoadLibraryEx(string lpFileName, IntPtr hFile, uint dwFlags);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
    public static extern IntPtr GetProcAddress(IntPtr hModule, string procName);

    [DllImport("kernel32.dll")]
    public static extern bool FreeLibrary(IntPtr hModule);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
    public static extern bool SetDllDirectory(string lpPathName);
}

[StructLayout(LayoutKind.Sequential)]
internal struct PASSTHRU_MSG
{
    public uint ProtocolID;
    public uint RxStatus;
    public uint TxFlags;
    public uint Timestamp;
    public uint DataSize;
    public uint ExtraDataIndex;

    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 4128)]
    public byte[] Data;

    public static PASSTHRU_MSG Empty(uint protocolId)
    {
        return new PASSTHRU_MSG
        {
            ProtocolID = protocolId,
            RxStatus = 0,
            TxFlags = 0,
            Timestamp = 0,
            DataSize = 0,
            ExtraDataIndex = 0,
            Data = new byte[4128],
        };
    }
}

internal static class J2534Const
{
    // Protocol IDs — базовые из спецификации SAE J2534-1.
    public const uint CAN = 5;
    public const uint ISO15765 = 6;

    // Connect flags
    public const uint CAN_29BIT_ID = 0x00000100;

    // Filter types
    public const uint PASS_FILTER = 0x00000001;

    // Статусы возврата (SAE J2534-1 v04.04, стандартная таблица кодов)
    public const int STATUS_NOERROR = 0x00;
    public const int ERR_FAILED = 0x07;
    public const int ERR_DEVICE_NOT_CONNECTED = 0x08;
    public const int ERR_TIMEOUT = 0x09;
    public const int ERR_BUFFER_EMPTY = 0x10;
    public const int ERR_BUFFER_FULL = 0x11;
}

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruOpen_t(IntPtr pName, ref uint pDeviceID);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruClose_t(uint deviceID);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruConnect_t(uint deviceID, uint protocolID, uint flags, uint baudRate, ref uint pChannelID);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruDisconnect_t(uint channelID);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruReadMsgs_t(uint channelID, ref PASSTHRU_MSG pMsg, ref uint pNumMsgs, uint timeout);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruWriteMsgs_t(uint channelID, ref PASSTHRU_MSG pMsg, ref uint pNumMsgs, uint timeout);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruStartMsgFilter_t(uint channelID, uint filterType, ref PASSTHRU_MSG pMaskMsg, ref PASSTHRU_MSG pPatternMsg, IntPtr pFlowControlMsg, ref uint pFilterID);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruStopMsgFilter_t(uint channelID, uint filterID);

[UnmanagedFunctionPointer(CallingConvention.StdCall)]
internal delegate int PassThruGetLastError_t(byte[] pErrorDescription);

/// <summary>
/// Обёртка над одной загруженной J2534 DLL: резолвит нужные функции и
/// даёт удобные C#-методы, кидающие исключения с текстом ошибки устройства.
/// </summary>
internal sealed class J2534Api : IDisposable
{
    private readonly IntPtr _module;

    private readonly PassThruOpen_t _open;
    private readonly PassThruClose_t _close;
    private readonly PassThruConnect_t _connect;
    private readonly PassThruDisconnect_t _disconnect;
    private readonly PassThruReadMsgs_t _readMsgs;
    private readonly PassThruWriteMsgs_t _writeMsgs;
    private readonly PassThruStartMsgFilter_t _startMsgFilter;
    private readonly PassThruStopMsgFilter_t? _stopMsgFilter;
    private readonly PassThruGetLastError_t? _getLastError;

    public J2534Api(string dllPath)
    {
        // Многие J2534 DLL (в т.ч. Scanmatik) сами подгружают соседние DLL
        // из своей папки (например SMJ2534.DLL/hklib.dll/...). Обычный
        // LoadLibrary их не найдёт — нужно явно расширить путь поиска.
        string? dir = System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(dllPath));
        if (!string.IsNullOrEmpty(dir))
            NativeLibrary.SetDllDirectory(dir);

        _module = NativeLibrary.LoadLibraryEx(dllPath, IntPtr.Zero, NativeLibrary.LOAD_WITH_ALTERED_SEARCH_PATH);
        if (_module == IntPtr.Zero)
        {
            int err = Marshal.GetLastWin32Error();
            throw new InvalidOperationException(
                $"Не удалось загрузить {dllPath} (Win32 error {err}). " +
                "Если ошибка 193 — это НЕ 32-битная DLL или мост собран не как win-x86.");
        }

        _open = GetDelegate<PassThruOpen_t>("PassThruOpen");
        _close = GetDelegate<PassThruClose_t>("PassThruClose");
        _connect = GetDelegate<PassThruConnect_t>("PassThruConnect");
        _disconnect = GetDelegate<PassThruDisconnect_t>("PassThruDisconnect");
        _readMsgs = GetDelegate<PassThruReadMsgs_t>("PassThruReadMsgs");
        _writeMsgs = GetDelegate<PassThruWriteMsgs_t>("PassThruWriteMsgs");
        _startMsgFilter = GetDelegate<PassThruStartMsgFilter_t>("PassThruStartMsgFilter");
        _stopMsgFilter = TryGetDelegate<PassThruStopMsgFilter_t>("PassThruStopMsgFilter");
        _getLastError = TryGetDelegate<PassThruGetLastError_t>("PassThruGetLastError");
    }

    private T GetDelegate<T>(string name) where T : Delegate
    {
        var d = TryGetDelegate<T>(name);
        if (d is null)
            throw new InvalidOperationException($"В DLL не найдена обязательная функция {name}.");
        return d;
    }

    private T? TryGetDelegate<T>(string name) where T : Delegate
    {
        IntPtr p = NativeLibrary.GetProcAddress(_module, name);
        if (p == IntPtr.Zero) return null;
        return Marshal.GetDelegateForFunctionPointer<T>(p);
    }

    public string GetLastErrorText()
    {
        if (_getLastError is null) return "(PassThruGetLastError недоступна в этой DLL)";
        var buf = new byte[80];
        int rc = _getLastError(buf);
        if (rc != J2534Const.STATUS_NOERROR) return "(не удалось получить текст ошибки)";
        int len = Array.IndexOf(buf, (byte)0);
        if (len < 0) len = buf.Length;
        return Encoding.ASCII.GetString(buf, 0, len);
    }

    private void ThrowIfError(int rc, string what)
    {
        if (rc != J2534Const.STATUS_NOERROR)
            throw new J2534Exception(rc, $"{what} -> код 0x{rc:X2}: {GetLastErrorText()}");
    }

    public uint Open()
    {
        uint deviceId = 0;
        int rc = _open(IntPtr.Zero, ref deviceId);
        ThrowIfError(rc, "PassThruOpen");
        return deviceId;
    }

    public void Close(uint deviceId)
    {
        _close(deviceId); // при закрытии ошибки только логируем, не бросаем
    }

    public uint Connect(uint deviceId, uint protocolId, uint flags, uint baudRate)
    {
        uint channelId = 0;
        int rc = _connect(deviceId, protocolId, flags, baudRate, ref channelId);
        ThrowIfError(rc, "PassThruConnect");
        return channelId;
    }

    public void Disconnect(uint channelId)
    {
        _disconnect(channelId);
    }

    /// <summary>
    /// Фильтр "пропускать всё" — без него ReadMsgs ничего не вернёт.
    /// flags — те же flags, что были переданы в Connect (важно передать сюда
    /// CAN_29BIT_ID, если канал открыт как 29-битный, иначе драйвер отклонит
    /// фильтр как "11-битный на 29-битном канале").
    /// </summary>
    public void StartPassAllFilter(uint channelId, uint protocolId, uint flags)
    {
        var mask = PASSTHRU_MSG.Empty(protocolId);
        var pattern = PASSTHRU_MSG.Empty(protocolId);
        mask.DataSize = 4;
        pattern.DataSize = 4;
        mask.TxFlags = flags;
        pattern.TxFlags = flags;
        // Data уже заполнен нулями -> маска 0x00000000 пропускает любой ID.
        uint filterId = 0;
        int rc = _startMsgFilter(channelId, J2534Const.PASS_FILTER, ref mask, ref pattern, IntPtr.Zero, ref filterId);
        ThrowIfError(rc, "PassThruStartMsgFilter");
    }

    /// <summary>Читает одно сообщение. Возвращает null, если за timeout ничего не пришло.</summary>
    public PASSTHRU_MSG? ReadOne(uint channelId, uint protocolId, uint timeoutMs)
    {
        var msg = PASSTHRU_MSG.Empty(protocolId);
        uint numMsgs = 1;
        int rc = _readMsgs(channelId, ref msg, ref numMsgs, timeoutMs);

        if (rc == J2534Const.STATUS_NOERROR && numMsgs >= 1) return msg;
        if (rc == J2534Const.ERR_BUFFER_EMPTY || rc == J2534Const.ERR_TIMEOUT) return null;

        throw new J2534Exception(rc, $"PassThruReadMsgs -> код 0x{rc:X2}: {GetLastErrorText()}");
    }

    /// <summary>
    /// Отправляет один CAN-кадр. canId — полный 29-битный (или 11-битный) ID,
    /// payload — данные кадра БЕЗ самого ID (обычно 8 байт для классического
    /// CAN с ISO-TP: PCI + данные + паддинг нулями).
    /// flags — те же TxFlags, что и при Connect (важно CAN_29BIT_ID для J1939).
    /// </summary>
    public void WriteOne(uint channelId, uint protocolId, uint canId, byte[] payload, uint flags, uint timeoutMs)
    {
        var msg = PASSTHRU_MSG.Empty(protocolId);
        msg.TxFlags = flags;
        msg.DataSize = (uint)(4 + payload.Length);
        msg.Data[0] = (byte)((canId >> 24) & 0xFF);
        msg.Data[1] = (byte)((canId >> 16) & 0xFF);
        msg.Data[2] = (byte)((canId >> 8) & 0xFF);
        msg.Data[3] = (byte)(canId & 0xFF);
        Array.Copy(payload, 0, msg.Data, 4, payload.Length);

        uint numMsgs = 1;
        int rc = _writeMsgs(channelId, ref msg, ref numMsgs, timeoutMs);
        ThrowIfError(rc, "PassThruWriteMsgs");
    }

    /// <summary>То же самое, но с сырым кодом возврата — только для диагностики.</summary>
    public (int rc, uint numMsgs, PASSTHRU_MSG msg) ReadOneRaw(uint channelId, uint protocolId, uint timeoutMs)
    {
        var msg = PASSTHRU_MSG.Empty(protocolId);
        uint numMsgs = 1;
        int rc = _readMsgs(channelId, ref msg, ref numMsgs, timeoutMs);
        return (rc, numMsgs, msg);
    }

    public void Dispose()
    {
        if (_module != IntPtr.Zero) NativeLibrary.FreeLibrary(_module);
    }
}

internal sealed class J2534Exception : Exception
{
    public int Code { get; }
    public J2534Exception(int code, string message) : base(message) { Code = code; }
}
