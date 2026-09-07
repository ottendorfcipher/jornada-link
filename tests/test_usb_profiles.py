import json

from jornada import usb_profiles as p


def test_table_keys_unique_and_json_is_ascii():
    keys = [d.key for d in p.DRIVER_PROFILES]
    assert len(keys) == len(set(keys))
    ids = [(d.vendor_id, d.product_id) for d in p.DRIVER_PROFILES]
    assert len(ids) == len(set(ids))
    text = p.table_json()
    assert text.isascii()
    data = json.loads(text)
    assert set(data) == {"drivers", "handhelds", "dock_markers"}
    assert [d["key"] for d in data["drivers"]] == sorted(d["key"] for d in data["drivers"])
    assert data["drivers"][0].keys() == {"key", "vendor_id", "product_id", "name", "chip", "role",
                                         "macos_driver", "driver_name", "notes"}


def test_classify_common_bridges():
    ftdi = p.classify(0x0403, 0x6001, "FT232R USB UART", "AB12")
    assert ftdi and ftdi.role == p.ROLE_SERIAL_BRIDGE
    assert ftdi.profile.macos_driver == p.DRIVER_BUILT_IN and ftdi.profile.driver_name == "AppleUSBFTDI"
    prolific = p.classify(0x067B, 0x2303)
    assert prolific and prolific.profile.macos_driver == p.DRIVER_VENDOR
    assert p.classify(0x10C4, 0xEA60).profile.chip == "CP210x"
    assert p.classify(0x1A86, 0x7523).profile.chip == "CH340"
    assert p.classify(0x0403, 0x6015).profile.key == "ftdi-ft-x"


def test_dock_marker_upgrades_a_bridge_but_not_a_sync_device():
    dock = p.classify(0x0403, 0x6001, "Jornada Dock Bridge", "JDOCK0001")
    assert dock and dock.role == p.ROLE_DOCK_BRIDGE
    assert p.classify(0x0403, 0x6001, "plain", "jornada-dock-2").role == p.ROLE_DOCK_BRIDGE
    sync = p.classify(0x03F0, 0x2016, "Jornada 720", "")
    assert sync and sync.role == p.ROLE_WINCE_USB_SYNC
    assert p.has_dock_marker(None, "") is False


def test_windows_ce_usb_sync_ids():
    for pid in (0x1016, 0x1116, 0x2016, 0x5216):
        found = p.classify(0x03F0, pid)
        assert found and found.role == p.ROLE_WINCE_USB_SYNC
        assert found.profile.macos_driver == p.DRIVER_NONE
    assert p.classify(0x049F, 0x0003).profile.key == "compaq-ipaq-sync"
    assert p.classify(0x045E, 0x00CE).role == p.ROLE_WINCE_USB_SYNC
    assert p.classify(0x0BB4, 0x00CE).role == p.ROLE_WINCE_USB_SYNC


def test_cdc_acm_by_interface_and_unknown_devices():
    acm = p.classify(0x2341, 0x0043, "Arduino Uno", interface_classes=[(2, 2), (10, 0)])
    assert acm and acm.profile.key == "usb-cdc-acm" and acm.role == p.ROLE_SERIAL_BRIDGE
    assert p.classify(0x2341, 0x0043, interface_classes=[(3, 1)]) is None
    assert p.classify(None, None) is None
    assert p.classify(0x2109, 0x2817, "USB2.0 Hub", interface_classes=[(9, 0)]) is None


def test_identify_handheld_from_dccm_strings():
    assert p.identify_handheld("SH3", "Jornada680").key == "jornada-680"
    assert p.identify_handheld("SH3", "HP Jornada 680e").key == "jornada-680e"
    assert p.identify_handheld("SH3", "Pocket_PC").key == "sh3-hpc-pro-family"
    assert p.identify_handheld("StrongARM", "My HPC").key == "sa1110-hpc2000-family"
    assert p.identify_handheld("SA1110", "Jornada 728").key == "jornada-728"
    assert p.identify_handheld("MIPS", "Mobilepro") is None
    assert p.identify_handheld(None, None) is None
    assert p.handheld("jornada-690e").name == "HP Jornada 690e"
    assert p.handheld("nope") is None


def test_capability_matrix_matches_the_silicon():
    for model in p.HANDHELD_MODELS:
        if model.architecture == p.ARCH_SH3:
            assert model.usb_client == p.USB_CLIENT_NONE and model.dock_usb is False
            assert "SH7709A" in model.cpu
        else:
            assert model.architecture == p.ARCH_ARM
            assert model.usb_client == p.USB_CLIENT_SA1110 and model.dock_usb is True
    assert {m.key for m in p.HANDHELD_MODELS} >= {"jornada-680e", "jornada-720", "sh3-hpc-pro-family"}
