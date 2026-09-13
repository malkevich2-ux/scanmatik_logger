using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Threading;
using ScanmatikBridge;

// ---------------------------------------------------------------------
// smbridge.exe — маленький 32-битный мост между Python-GUI (64-бит) и
// 32-битной J2534 DLL прибора Scanmatik.
//
// Протокол общения — построчно через stdin/stdout (текст, ASCII).
//
// Команды (stdin), по одной в строке:
//   OPEN <путь_к_dll>
//   CONNECT <CAN|J1939> <скорость_бод>
//   START
//   STOP
//   CLOSE
//   SEND <can_id_hex> <data_hex_bytes...>   — отправить один CAN-кадр
//                                              (data — ровно столько байт,
//                                              сколько прислали, без паддинга)
//
// Ответы/события (stdout):
//   OK <текст>
//   ERR <текст>
//   FRAME <pc_ms> <dev_ts_us> <can_id_hex> <ide0/1> <dlc> <data_hex...> <rxstatus_hex>
//
// Процесс всегда завершается сам после CLOSE (или при ошибке stdin).
// ---------------------------------------------------------------------

// ВАЖНО: было Encoding.ASCII — из-за этого все русские сообщения об ошибках
// превращались в "?????" в выводе. Кодировка должна совпадать по обе стороны
// трубы: Python-сторона тоже должна открывать процесс с encoding="utf-8".
Console.OutputEncoding = Encoding.UTF8;
Console.InputEncoding = Encoding.UTF8;

J2534Api? api = null;
uint deviceId = 0;
uint channelId = 0;
uint protocolId = J2534Const.CAN;
uint connectFlags = 0;
bool connected = false;

Thread? readerThread = null;
var state = new LoggingState();

var swEpoch = Stopwatch.StartNew();
DateTime unixEpoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
// Точка отсчёта "часы ПК" фиксируется ОДИН раз здесь, дальше миллисекунды
// считаем через Stopwatch (см. NowMs() ниже) — так надёжнее.
long startEpochMs = (long)(DateTime.UtcNow - unixEpoch).TotalMilliseconds;

void Out(string line)
{
    Console.WriteLine(line);
    Console.Out.Flush();
}

// ---------------------------------------------------------------------
// НАЙДЕНА И ИСПРАВЛЕНА ГЛАВНАЯ ПРИЧИНА "повреждённых обменов" (потери
// Consecutive Frame при многокадровой пересборке ISO-TP на плотной шине).
//
// Было: ReaderLoop сам вызывал Out() — то есть Console.WriteLine +
// Console.Out.Flush() — СИНХРОННО, на каждый отдельный кадр, в том же
// потоке, что читает PassThruReadMsgs. Flush — это системный вызов записи в
// трубу (pipe) к Python-процессу; если Python на миг не успевает читать
// (труба заполнилась — а её буфер небольшой, обычно единицы КБ), Flush()
// БЛОКИРУЕТСЯ. Пока он заблокирован, поток чтения НЕ вызывает
// PassThruReadMsgs — а у самого J2534-драйвера/адаптера свой буфер
// КОНЕЧНОГО размера, который в этот момент продолжает наполняться
// реальными кадрами с шины. Если пауза случилась в середине пачки
// Consecutive Frame одного многокадрового UDS-обмена — один или несколько
// CF реально теряются на уровне драйвера, и потом это видно как "разрыв
// последовательности" при пересборке (см. IsoTpReassembler в uds_decoder.py).
//
// Стало: поток чтения J2534 (ReaderLoop) больше НЕ ждёт запись в трубу —
// он только форматирует строку и кладёт её в очередь в памяти (frameQueue),
// это быстро и никогда не блокируется на I/O. Отдельный поток-писатель
// (WriterLoop) разбирает эту очередь и пишет в stdout, с флашем не на
// каждую строку, а когда очередь опустела (тот же результат по задержке
// вывода, но без блокировки самого чтения с шины).
var frameQueue = new BlockingCollection<string>(boundedCapacity: 50_000);

void QueueFrame(string line)
{
    if (!frameQueue.TryAdd(line))
    {
        // Практически недостижимо (50k кадров в очереди разом) — но если
        // всё-таки уткнулись, не блокируем чтение с шины намертво:
        // жертвуем этим одним кадром вместо повторения старой проблемы.
    }
}

Thread writerThread = new Thread(() =>
{
    foreach (var line in frameQueue.GetConsumingEnumerable())
    {
        Console.WriteLine(line);
        if (frameQueue.Count == 0) Console.Out.Flush();
    }
}) { IsBackground = true, Name = "smbridge-writer" };
writerThread.Start();

// ВАЖНО: раньше здесь был (long)(DateTime.UtcNow - unixEpoch).TotalMilliseconds
// на каждый кадр. На Windows системные часы, которые читает DateTime.UtcNow,
// по умолчанию обновляются примерно раз в ~15 мс (период системного
// таймера) — а не при каждом обращении. Из-за этого несколько кадров,
// реально пришедших с интервалом в 1-5 мс, получали ОДИНАКОВЫЙ pc_ms в
// FRAME-строке — в логе (CSV) это выглядит как "задвоившийся" кадр с
// абсолютно одинаковым временем, хотя на самом деле ничего не потерялось и
// не задублировалось, просто у часов не хватило разрешения различить их.
// Stopwatch читает аппаратный счётчик (QueryPerformanceCounter) заново при
// каждом обращении — разрешение на порядки выше, поэтому берём эпоху один
// раз при старте и дальше считаем миллисекунды через Stopwatch.
long NowMs() => startEpochMs + swEpoch.ElapsedMilliseconds;

void ReaderLoop(uint chId, uint protoId)
{
    while (state.Logging)
    {
        try
        {
            var msg = api!.ReadOne(chId, protoId, 100);
            if (msg is null) continue;

            var m = msg.Value;
            if (m.DataSize < 4) continue; // некорректное сообщение без CAN ID

            uint canId = (uint)((m.Data[0] << 24) | (m.Data[1] << 16) | (m.Data[2] << 8) | m.Data[3]);
            int payloadLen = (int)m.DataSize - 4;
            if (payloadLen < 0) payloadLen = 0;

            var sb = new StringBuilder();
            for (int i = 0; i < payloadLen; i++)
                sb.Append(m.Data[4 + i].ToString("X2")).Append(payloadLen - i > 1 ? " " : "");

            bool ide = (m.RxStatus & J2534Const.CAN_29BIT_ID) != 0;

            // QueueFrame, а НЕ Out() — см. комментарий у frameQueue выше:
            // этот вызов не должен блокироваться на записи в трубу, иначе
            // мы снова перестанем вызывать PassThruReadMsgs вовремя.
            QueueFrame($"FRAME {NowMs()} {m.Timestamp} {canId:X} {(ide ? 1 : 0)} {payloadLen} {sb} {m.RxStatus:X}");
        }
        catch (J2534Exception jex)
        {
            Out($"ERR read {jex.Code:X2} {jex.Message}");
            // Не убиваем поток на единичной ошибке чтения — пробуем ещё,
            // но чтобы не заспамить лог, чуть притормозим.
            Thread.Sleep(200);
        }
        catch (Exception ex)
        {
            Out($"ERR read-fatal {ex.Message}");
            state.Logging = false;
        }
    }
}

Out("INFO smbridge ready");

string? line;
while ((line = Console.ReadLine()) != null)
{
    line = line.Trim();
    if (line.Length == 0) continue;
    var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
    var cmd = parts[0].ToUpperInvariant();

    try
    {
        switch (cmd)
        {
            case "OPEN":
            {
                if (parts.Length < 2) { Out("ERR OPEN нужен путь к dll"); break; }
                string dllPath = line.Substring(5).Trim();
                api?.Dispose();
                api = new J2534Api(dllPath);
                deviceId = api.Open();
                Out($"OK OPEN device={deviceId}");
                break;
            }

            case "CONNECT":
            {
                if (api is null) { Out("ERR CONNECT нужно сначала OPEN"); break; }
                if (parts.Length < 3) { Out("ERR CONNECT <CAN|J1939> <baud> [NOFILTER]"); break; }

                string mode = parts[1].ToUpperInvariant();
                if (!uint.TryParse(parts[2], out uint baud)) { Out("ERR некорректная скорость"); break; }
                bool noFilter = parts.Length >= 4 && parts[3].Equals("NOFILTER", StringComparison.OrdinalIgnoreCase);

                protocolId = J2534Const.CAN;
                uint flags = mode == "J1939" ? J2534Const.CAN_29BIT_ID : 0u;

                uint newChannelId = api.Connect(deviceId, protocolId, flags, baud);
                try
                {
                    if (!noFilter)
                        api.StartPassAllFilter(newChannelId, protocolId, flags);
                }
                catch
                {
                    // Канал открылся, а фильтр не встал — не оставляем висящий
                    // канал, иначе следующий CONNECT будет падать с странными
                    // ошибками (PIN_INVALID/CHANNEL_IN_USE и т.п.).
                    api.Disconnect(newChannelId);
                    throw;
                }

                channelId = newChannelId;
                connectFlags = flags;
                connected = true;
                Out($"OK CONNECT channel={channelId} mode={mode} baud={baud} filter={!noFilter}");
                break;
            }

            case "SEND":
            {
                if (!connected || api is null) { Out("ERR SEND нужно сначала CONNECT"); break; }
                if (parts.Length < 3) { Out("ERR SEND <can_id_hex> <data_hex_bytes...>"); break; }

                if (!uint.TryParse(parts[1], System.Globalization.NumberStyles.HexNumber, null, out uint sendCanId))
                {
                    Out("ERR SEND некорректный CAN ID");
                    break;
                }

                var dataBytes = new byte[parts.Length - 2];
                bool parseOk = true;
                for (int i = 2; i < parts.Length; i++)
                {
                    if (!byte.TryParse(parts[i], System.Globalization.NumberStyles.HexNumber, null, out dataBytes[i - 2]))
                    {
                        parseOk = false;
                        break;
                    }
                }
                if (!parseOk) { Out("ERR SEND некорректные данные (ожидались hex-байты)"); break; }

                api.WriteOne(channelId, protocolId, sendCanId, dataBytes, connectFlags, 1000);
                Out($"OK SEND {sendCanId:X} {dataBytes.Length}");
                break;
            }

            case "DIAG":
            {
                // Диагностика: печатает КАЖДЫЙ вызов PassThruReadMsgs как есть,
                // включая "пусто" — чтобы понять, реально ли драйвер вызывается
                // и что именно отвечает, без нашей обычной фильтрации ошибок.
                if (!connected || api is null) { Out("ERR DIAG нужно сначала CONNECT"); break; }
                uint diagMs = 3000;
                if (parts.Length >= 2) uint.TryParse(parts[1], out diagMs);

                var swDiag = Stopwatch.StartNew();
                int calls = 0, empties = 0, frames = 0, errors = 0;
                while (swDiag.ElapsedMilliseconds < diagMs)
                {
                    calls++;
                    var (rc, numMsgs, msg) = api.ReadOneRaw(channelId, protocolId, 200);
                    if (rc == J2534Const.STATUS_NOERROR && numMsgs >= 1)
                    {
                        frames++;
                        var sbd = new StringBuilder();
                        int n = Math.Min((int)msg.DataSize, 16);
                        for (int i = 0; i < n; i++) sbd.Append(msg.Data[i].ToString("X2")).Append(' ');
                        Out($"DIAG FRAME rc=0x{rc:X2} numMsgs={numMsgs} size={msg.DataSize} rxstatus=0x{msg.RxStatus:X} data={sbd}");
                    }
                    else if (rc == J2534Const.ERR_BUFFER_EMPTY || rc == J2534Const.ERR_TIMEOUT)
                    {
                        empties++;
                        if (empties <= 3 || empties % 20 == 0)
                            Out($"DIAG EMPTY rc=0x{rc:X2} numMsgs={numMsgs} (call #{calls})");
                    }
                    else
                    {
                        errors++;
                        Out($"DIAG ERR rc=0x{rc:X2} numMsgs={numMsgs} text={api.GetLastErrorText()} (call #{calls})");
                    }
                }
                Out($"OK DIAG calls={calls} frames={frames} empties={empties} errors={errors}");
                break;
            }

            case "START":
            {
                if (!connected || api is null) { Out("ERR START нужно сначала CONNECT"); break; }
                if (state.Logging) { Out("OK START уже идёт"); break; }
                state.Logging = true;
                readerThread = new Thread(() => ReaderLoop(channelId, protocolId)) { IsBackground = true };
                readerThread.Start();
                Out("OK START");
                break;
            }

            case "STOP":
            {
                state.Logging = false;
                readerThread?.Join(2000);
                readerThread = null;
                if (api is not null && connected)
                {
                    api.Disconnect(channelId);
                    connected = false;
                }
                Out("OK STOP");
                break;
            }

            case "CLOSE":
            {
                state.Logging = false;
                readerThread?.Join(2000);
                readerThread = null;
                if (api is not null)
                {
                    if (connected) { api.Disconnect(channelId); connected = false; }
                    api.Close(deviceId);
                    api.Dispose();
                    api = null;
                }
                Out("OK CLOSE");
                Console.Out.Flush();
                frameQueue.CompleteAdding();
                Environment.Exit(0);
                break;
            }

            default:
                Out($"ERR неизвестная команда: {cmd}");
                break;
        }
    }
    catch (J2534Exception jex)
    {
        Out($"ERR {cmd} {jex.Code:X2} {jex.Message}");
    }
    catch (Exception ex)
    {
        Out($"ERR {cmd} exception {ex.Message}");
    }
}

// stdin закрылся (Python-процесс завершился) — подчищаем и выходим.
try
{
    state.Logging = false;
    readerThread?.Join(1000);
    if (api is not null)
    {
        if (connected) api.Disconnect(channelId);
        api.Close(deviceId);
        api.Dispose();
    }
}
catch { /* уже выходим, ошибки тут не важны */ }

// В top-level statements локальная переменная не может быть volatile —
// поэтому флаг работы reader-потока вынесен в отдельный маленький класс.
internal sealed class LoggingState
{
    private volatile bool _logging;
    public bool Logging
    {
        get => _logging;
        set => _logging = value;
    }
}
