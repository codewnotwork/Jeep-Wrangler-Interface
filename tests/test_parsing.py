import pytest

from wrangler_obd.dtc import decode_dtc, decode_dtc_payload, describe
from wrangler_obd.elm327 import ElmError, parse_response
from wrangler_obd.pids import PIDS, decode_monitor_status, decode_supported, to_imperial


def test_single_frame_with_spaces():
    assert parse_response("41 0C 1A F8\r\r>") == [[0x41, 0x0C, 0x1A, 0xF8]]


def test_multiple_ecus_one_line_each():
    assert parse_response("410C1AF8\r410C1B00") == [[0x41, 0x0C, 0x1A, 0xF8], [0x41, 0x0C, 0x1B, 0x00]]


def test_multiframe_vin():
    raw = "014\r0:490201314A34\r1:41413244313041\r2:4C303030303030"
    (msg,) = parse_response(raw)
    assert len(msg) == 0x14
    assert bytes(msg[3:]).decode() == "1J4AA2D10AL000000"


def test_no_data_and_searching_are_skipped():
    assert parse_response("SEARCHING...\rNO DATA") == []


def test_adapter_errors_raise():
    with pytest.raises(ElmError):
        parse_response("UNABLE TO CONNECT")


@pytest.mark.parametrize(
    "key,data,expected",
    [
        ("rpm", [0x1A, 0xF8], 1726.0),
        ("coolant_temp", [0x87], 95.0),
        ("speed", [0x50], 80.0),
        ("ltft_b1", [0x88], 6.25),
        ("stft_b2", [0x78], -6.25),
        ("timing_advance", [0x98], 12.0),
        ("maf", [0x01, 0x40], 3.2),
        ("module_voltage", [0x37, 0x78], 14.2),
        ("o2_b1s1", [0x5A, 0x80], 0.45),
    ],
)
def test_pid_decoders(key, data, expected):
    assert PIDS[key].decode(data) == pytest.approx(expected)


def test_supported_bitmap():
    # 0xBE1FA813 is a classic example: PIDs 01,03-07,0C-11,13,15,1C,1F,20
    s = decode_supported(0x00, [0xBE, 0x1F, 0xA8, 0x13])
    assert {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x13, 0x15, 0x1C, 0x1F, 0x20} == s


def test_monitor_status():
    assert decode_monitor_status([0x83, 0x07, 0x65, 0x00]) == {"mil_on": True, "dtc_count": 3}


def test_imperial():
    assert to_imperial(100.0, "°C") == (pytest.approx(212.0), "°F")
    assert to_imperial(5.0, "%") == (5.0, "%")


@pytest.mark.parametrize(
    "a,b,code",
    [(0x04, 0x56, "P0456"), (0x01, 0x71, "P0171"), (0x12, 0x81, "P1281"), (0xC1, 0x00, "U0100"), (0x41, 0x23, "C0123")],
)
def test_decode_dtc(a, b, code):
    assert decode_dtc(a, b) == code


def test_dtc_payload_can_with_count_byte():
    assert decode_dtc_payload([0x02, 0x04, 0x56, 0x01, 0x71]) == ["P0456", "P0171"]


def test_dtc_payload_legacy_padded():
    assert decode_dtc_payload([0x04, 0x56, 0x00, 0x00, 0x00, 0x00]) == ["P0456"]


def test_describe():
    assert "SKIM" in describe("P0513")
    assert "Manufacturer" in describe("P1999")


class FakeSerial:
    """Answers with a prompt only at one baud rate."""

    def __init__(self, live_rate):
        self.live_rate = live_rate
        self.baudrate = 0
        self._buf = b""

    @property
    def in_waiting(self):
        return len(self._buf)

    def reset_input_buffer(self):
        self._buf = b""

    def write(self, data):
        self._buf = b"\r>" if self.baudrate == self.live_rate else b"\x00\xff"

    def flush(self):
        pass

    def read(self, n):
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def test_detect_baud_finds_live_rate():
    from wrangler_obd.transport import detect_baud

    ser = FakeSerial(38400)
    assert detect_baud(ser, probe_timeout=0.01) == 38400
    assert ser.baudrate == 38400


def test_detect_baud_none_when_silent():
    from wrangler_obd.transport import detect_baud

    assert detect_baud(FakeSerial(1), rates=(9600, 115200), probe_timeout=0.01) is None
