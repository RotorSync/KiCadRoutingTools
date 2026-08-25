#!/usr/bin/env python3
"""#SI-phase1: net noise classifier (py_router/si_classes.py) unit tests.

The classifier turns every net on a board into AGGRESSOR / VICTIM / NEUTRAL
(SHIELD for ground planes) so that coupling between noisy and sensitive lines
becomes measurable. These tests pin:

  1. The documented pattern tables on hand-labeled net names (the "name
     heuristic" contract).
  2. The priority order: override file > switch-node hint > netclass > pad
     metadata > net name > NEUTRAL.
  3. Board-level classification on real boards (routed_output.kicad_pcb and
     the carrier boards), with an honest precision report: name heuristics miss
     nets that carry no name signal, and the per-board override file
     (<board>.si.json) is the escape hatch that always wins.

Run with:  python3 tests/test_si_classes.py
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))
sys.path.insert(0, os.path.join(ROOT_DIR, 'rust_router'))

import si_classes as si  # noqa: E402

FAILURES = []


def check(cond, label, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 1. Name-pattern table on hand-labeled nets
# ---------------------------------------------------------------------------

# (net_name, expected_class) -- hand-labeled from d1.kicad_pcb's real nets.
# These are the nets an experienced designer would call noisy (AGGRESSOR) or
# sensitive (VICTIM) purely from the name.
NAME_CASES = [
    # --- AGGRESSOR: switching-regulator nodes ---
    ('Net-(U5-SW)', si.AGGRESSOR),
    ('Net-(U4-RT{slash}CLK)', si.AGGRESSOR),   # RT/CLK -- switching frequency pin
    ('Net-(U5-BST)', si.AGGRESSOR),            # bootstrap
    ('Net-(U3-VIN)', si.AGGRESSOR),            # regulator input branch
    ('VIN_RAW', si.AGGRESSOR),
    ('VBULK', si.AGGRESSOR),
    ('VOUT_PD', si.AGGRESSOR),
    ('VBUS_IN', si.AGGRESSOR),
    ('VBUS', si.AGGRESSOR),
    ('VIN_PROT', si.AGGRESSOR),
    ('Net-(U6-VREG_LX)', si.AGGRESSOR),        # rp2350 buck switch node (LX)
    ('Net-(U7-SW)', si.AGGRESSOR),             # rp2350 buck switch node (SW)
    ('PUSB_VBUS', si.AGGRESSOR),               # orangecrab USB VBUS branch
    ('REF_CLK', si.AGGRESSOR),                 # reference clock
    ('SD0_CLK', si.AGGRESSOR),                 # SD clock (aggressive)
    ('EXT_PLL+', si.AGGRESSOR),                # PLL reference clock
    ('EXT_PLL-', si.AGGRESSOR),
    ('/RP2354A/FPGA.CLK', si.AGGRESSOR),
    ('/RP2354A/FPGA.OSC_EN', si.AGGRESSOR),    # oscillator enable (clock-ish)
    # --- VICTIM: serial data ---
    ('UART0_TX', si.VICTIM),
    ('UART0_RX', si.VICTIM),
    ('SPI0_MOSI', si.VICTIM),
    ('SPI0_MISO', si.VICTIM),
    ('SPI0_SCLK', si.VICTIM),
    ('SPI0_CE1_ADC1', si.VICTIM),              # chip-select -> strap/serial control
    ('SCL1', si.VICTIM),
    ('SDA1', si.VICTIM),
    ('PD_EE_SCL', si.VICTIM),
    ('PD_EE_SDA', si.VICTIM),
    ('CAN_TX', si.VICTIM),
    ('CAN_RX', si.VICTIM),
    ('CAN_H', si.VICTIM),
    ('CAN_L', si.VICTIM),
    ('CAN_H_BUS', si.VICTIM),
    ('CAN_L_BUS', si.VICTIM),
    ('ISO_MOSI', si.VICTIM),
    ('ISO_MISO', si.VICTIM),
    ('ISO_SCLK', si.VICTIM),
    ('SSRX1_P', si.VICTIM),                    # USB3 super-speed RX
    ('SSRX2_N', si.VICTIM),
    ('SSTX1_P', si.VICTIM),                    # USB3 super-speed TX
    ('SSTX2_N', si.VICTIM),
    ('USB2_P', si.VICTIM),
    ('USB2_N', si.VICTIM),
    ('IPAD_DM', si.VICTIM),                    # USB D-
    ('IPAD_DP', si.VICTIM),                    # USB D+
    # --- VICTIM: analog sense ---
    ('TC1_AN', si.VICTIM),                     # thermocouple channel
    ('TC13_AP', si.VICTIM),
    ('AUX_V_ADC', si.VICTIM),
    ('AUX_I_ADC', si.VICTIM),
    ('AUX_EXC_SENSE', si.VICTIM),
    ('DIN_SENSE', si.VICTIM),
    ('ADC1_nDRDY', si.VICTIM),                 # ADC data-ready (serial-ish)
    ('ADC2_nCS', si.VICTIM),                   # ADC chip-select
    # --- VICTIM: radio / reset ---
    ('FPGA_RESET', si.VICTIM),
    ('RAM_RESET#', si.VICTIM),
    ('Net-(J2-~{RESET})', si.VICTIM),
    ('/RP2354A/FPGA.~{RESET}', si.VICTIM),
    # --- NEUTRAL: quiet rails / misc ---
    ('+3V3', si.NEUTRAL),
    ('+5V', si.NEUTRAL),
    ('CM4_3V3', si.NEUTRAL),
    ('ISO_3V3', si.NEUTRAL),
    ('GLOBAL_EN', si.NEUTRAL),
    ('EN54560', si.NEUTRAL),
    ('ENDIV', si.NEUTRAL),
    ('GND', si.NEUTRAL),
    ('GNDA', si.NEUTRAL),
    ('MUX_SEL', si.NEUTRAL),
]


def test_name_patterns():
    print("name-pattern table (hand-labeled d1/carrier nets)")
    for name, expected in NAME_CASES:
        got = si.classify_net(name)
        check(got == expected, f"'{name}' -> {expected}",
              f"got {got}")


# ---------------------------------------------------------------------------
# 2. Priority order
# ---------------------------------------------------------------------------

def test_priority():
    print("priority order: override > switch hint > netclass > metadata > name")
    # override wins over everything
    check(si.classify_net('UART0_TX', override=si.NEUTRAL) == si.NEUTRAL,
          "override NEUTRAL beats name VICTIM")
    check(si.classify_net('+3V3', override=si.AGGRESSOR) == si.AGGRESSOR,
          "override AGGRESSOR beats name NEUTRAL")
    check(si.classify_net('UART0_TX', switch_hint=True, override=si.NEUTRAL) == si.NEUTRAL,
          "override beats switch hint")
    # switch hint beats name
    check(si.classify_net('+5V', switch_hint=True) == si.AGGRESSOR,
          "switch hint beats quiet-rail name")
    # netclass beats name
    check(si.classify_net('+5V', netclass='POWER') == si.AGGRESSOR,
          "POWER netclass beats quiet-rail name")
    check(si.classify_net('UART0_TX', netclass='CANBUS') == si.VICTIM,
          "CANBUS netclass -> VICTIM")
    # power-pin metadata does NOT make a rail VICTIM
    check(si.classify_net('+3V3', pinfunctions=['ADC_AVDD_44'],
                          pintypes=['power_in']) == si.NEUTRAL,
          "power pin function ADC_AVDD does not make rail VICTIM")
    check(si.classify_net('+1V1', pinfunctions=['VCCA_PLL_A1'],
                          pintypes=['power_in']) == si.NEUTRAL,
          "power pin function VCCA_PLL does not make rail VICTIM")
    # signal-pin metadata makes a net VICTIM even if the name is opaque
    check(si.classify_net('Net-(U2A-~{RESET})') == si.VICTIM,
          "reset name -> VICTIM")
    check(si.classify_net('some_opaque_name',
                          pinfunctions=['SCL'], pintypes=['bidirectional']) == si.VICTIM,
          "pinfunction SCL -> VICTIM even for opaque net name")
    # regulator power pin -> AGGRESSOR branch
    check(si.classify_net('some_opaque_sw',
                          pinfunctions=['SW'], pintypes=['power_out'],
                          on_regulator_footprint=True) == si.AGGRESSOR,
          "regulator SW power pin -> AGGRESSOR")
    # default NEUTRAL
    check(si.classify_net('Net-(CJ1-Pad1)') == si.NEUTRAL,
          "opaque default -> NEUTRAL")


# ---------------------------------------------------------------------------
# 3. Override file support
# ---------------------------------------------------------------------------

def test_overrides():
    print("override file: <board>.si.json always wins")
    import tempfile, json as _json

    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'x.kicad_pcb')
        ov = os.path.join(td, 'x.si.json')
        with open(ov, 'w') as f:
            _json.dump({'UART0_TX': 'NEUTRAL', '+5V': 'AGGRESSOR'}, f)
        got = si.load_overrides(board)
        check(got == {'UART0_TX': 'NEUTRAL', '+5V': 'AGGRESSOR'},
              "load_overrides reads JSON", str(got))
        check(si.override_path(board) == ov, "override_path is <board>.si.json")
        # malformed JSON -> empty dict, never crash
        with open(ov, 'w') as f:
            f.write('{not json')
        check(si.load_overrides(board) == {}, "malformed override -> empty dict")
        # missing file -> empty dict
        check(si.load_overrides(os.path.join(td, 'nope.kicad_pcb')) == {},
              "missing override -> empty dict")


# ---------------------------------------------------------------------------
# 4. Board-level classification + honest precision on real boards
# ---------------------------------------------------------------------------

def _hand_labels():
    """Hand-labeled expectations for routed_output.kicad_pcb's ROUTED nets.

    We only judge nets that carry copper (segments) -- those are the ones the
    coupling metric actually measures. The label is what a designer would say
    from the net name + the part it connects (per the parser's pad metadata).
    """
    return {
        # serial data (VICTIM): the FX2 (U2A) parallel FIFO + control bus is
        # digital data; the LVDS serial lanes feed the ADC.
        'Net-(U2A-DATA_0)': si.VICTIM,
        'Net-(U2A-DATA_1)': si.VICTIM,
        'Net-(U2A-~{SIWU})': si.NEUTRAL,   # FIFO "slave interface wakeup" -- status/control
        'Net-(U2A-~{WAKEUP})': si.NEUTRAL,  # FIFO wakeup -- status/control
        'Net-(U2A-CLK)': si.AGGRESSOR,     # FIFO clock -- aggressive clock line
        '/fpga_adc/lvds_rx1_1_P': si.VICTIM,   # LVDS serial data lane
        '/fpga_adc/lvds_rx1_1_N': si.VICTIM,
        '/fpga_adc/lvds_rx_top_clkin1_P': si.AGGRESSOR,  # LVDS clock lane
        '/fpga_adc/lvds_rx_top_clkin1_N': si.AGGRESSOR,
        '/fpga_adc/VA11': si.VICTIM,       # ADC analog supply sense? -- treat as VICTIM
        '/fpga_adc/VD11': si.VICTIM,
        'Net-(U1B-CK_t_A)': si.AGGRESSOR,  # DDR clock pair
        'Net-(U1B-CK_c_A)': si.AGGRESSOR,
        'Net-(U1B-CA0_A)': si.VICTIM,      # DDR command/address -- serial-ish
        'Net-(U1A-DQ0_A)': si.VICTIM,      # DDR data -- serial-ish
        'Net-(U1A-DQS0_t_A)': si.AGGRESSOR,  # DDR data strobe (clock-like)
        'Net-(U1A-DQS0_c_A)': si.AGGRESSOR,
        'GND': si.NEUTRAL,
        '/fpga_ddr/1V1': si.NEUTRAL,
        '/fpga_ddr/VDDQ': si.NEUTRAL,
        '/fpga_ddr/VDD1': si.NEUTRAL,
        '/fpga_ddr/VDD2': si.NEUTRAL,
        'Net-(U2A-GPIO0)': si.NEUTRAL,
        'Net-(U2A-GPIO1)': si.NEUTRAL,
        'Net-(U2A-~{OE})': si.NEUTRAL,     # output-enable -- control, not serial
        'Net-(U2A-~{RD})': si.NEUTRAL,
        'Net-(U2A-~{WR})': si.NEUTRAL,
        'Net-(U2A-BE_0)': si.NEUTRAL,      # byte-enable -- control
        'Net-(U2A-BE_1)': si.NEUTRAL,
        'Net-(U2A-BE_2)': si.NEUTRAL,
        'Net-(U2A-BE_3)': si.NEUTRAL,
        'unconnected-(IC1D-CLK+-PadH1)': si.AGGRESSOR,  # ADC clock input
        'unconnected-(IC1D-CLK--PadJ1)': si.AGGRESSOR,
        'unconnected-(IC1D-INA+-PadA4)': si.VICTIM,     # ADC analog input
        'unconnected-(IC1D-INA--PadA5)': si.VICTIM,
        'unconnected-(IC1E-SCLK-PadT8)': si.VICTIM,     # SPI clock to ADC
        'unconnected-(IC1E-SDO-PadN8)': si.VICTIM,      # SPI data out
        'unconnected-(IC1E-SDI-PadP8)': si.VICTIM,
        'unconnected-(IC1E-SCS-PadR8)': si.VICTIM,      # SPI chip select
        'unconnected-(U3F-spi_clk-PadV18)': si.VICTIM,
        'unconnected-(U3F-spi_miso-PadT20)': si.VICTIM,
        'unconnected-(U3F-spi_mosi-PadT19)': si.VICTIM,
        'unconnected-(U3F-lvdsin_clk-PadE2)': si.AGGRESSOR,
        'unconnected-(U3F-lvdsin_clk_N-PadE3)': si.AGGRESSOR,
        'unconnected-(U3F-lvdsin_trig-PadB2)': si.NEUTRAL,   # trigger -- control-ish
        'unconnected-(U3F-lvdsin_trig_N-PadA2)': si.NEUTRAL,
        'unconnected-(U3F-adc_clkout-PadU17)': si.AGGRESSOR,
        'unconnected-(U3F-main_pllin-PadW4)': si.AGGRESSOR,
        'unconnected-(U3F-ext_clkin-PadC5)': si.AGGRESSOR,
        'unconnected-(U3F-ddr_pllin-PadU18)': si.AGGRESSOR,
        'unconnected-(IC1E-SYNCSE-PadE2)': si.NEUTRAL,   # sync select -- control
        'unconnected-(IC1E-CALSTAT-PadB1)': si.NEUTRAL,  # cal status -- control
        'unconnected-(IC1E-ORA0-PadB8)': si.NEUTRAL,     # overrange alarm -- control
        'unconnected-(IC1E-ORA1-PadA8)': si.NEUTRAL,
        'unconnected-(IC1E-ORB0-PadD8)': si.NEUTRAL,
        'unconnected-(IC1E-ORB1-PadC8)': si.NEUTRAL,
        'unconnected-(IC1E-BG-PadD2)': si.NEUTRAL,       # bandgap -- quiet ref-ish
        'unconnected-(IC1E-TDIODE+-PadM2)': si.NEUTRAL,  # temp diode -- analog-ish
        'unconnected-(IC1E-TDIODE--PadN2)': si.NEUTRAL,
        'unconnected-(IC1E-TMSTP+-PadD1)': si.NEUTRAL,   # temp stop -- control
        'unconnected-(IC1E-TMSTP--PadE1)': si.NEUTRAL,
        'unconnected-(IC1D-INB+-PadT4)': si.VICTIM,      # ADC analog input B
        'unconnected-(IC1D-INB--PadT5)': si.VICTIM,
        'unconnected-(IC1D-SYSREF+-PadM1)': si.AGGRESSOR,  # ADC sysref clock
        'unconnected-(IC1D-SYSREF--PadN1)': si.AGGRESSOR,
        'unconnected-(IC1D-PD-PadR1)': si.NEUTRAL,       # power-down -- control
        'unconnected-(U2B-DM-Pad25)': si.VICTIM,         # USB D-
        'unconnected-(U2B-DP-Pad23)': si.VICTIM,         # USB D+
        'unconnected-(U2B-RIDN-Pad34)': si.NEUTRAL,      # resistor-id -- quiet
        'unconnected-(U2B-RIDP-Pad35)': si.NEUTRAL,
        'unconnected-(U2B-RREF-Pad27)': si.NEUTRAL,      # ref resistor -- quiet
        'unconnected-(U2B-TODP-Pad32)': si.NEUTRAL,      # test out -- quiet-ish
        'unconnected-(U2B-TODN-Pad31)': si.NEUTRAL,
        'unconnected-(U2B-XI-Pad21)': si.AGGRESSOR,      # crystal in -- clock-ish
        'unconnected-(U2B-XO-Pad22)': si.AGGRESSOR,
        'unconnected-(U2C-VBUS-Pad37)': si.AGGRESSOR,    # USB VBUS branch
        'unconnected-(U2C-AVDD-Pad2)': si.NEUTRAL,       # analog supply -- quiet rail
        'unconnected-(U2C-VCC33-Pad20)': si.NEUTRAL,
        'unconnected-(U2C-VCCIO-Pad14)': si.NEUTRAL,
        'unconnected-(U2C-VD10-Pad30)': si.NEUTRAL,
        'unconnected-(U2C-VD10-Pad48)': si.NEUTRAL,
        'unconnected-(U2C-VD10-Pad33)': si.NEUTRAL,
        'unconnected-(U2C-VD10-Pad3)': si.NEUTRAL,
        'unconnected-(U2C-VDDA-Pad28)': si.NEUTRAL,
        'unconnected-(U3H-VCC-PadJ11)': si.NEUTRAL,      # FPGA supply -- quiet rail
        'unconnected-(U3H-VCCAUX-PadJ16)': si.NEUTRAL,
        'unconnected-(U3H-VCCIO2A-PadP17)': si.NEUTRAL,
        'unconnected-(U3H-VCCIO33_BL-PadU4)': si.NEUTRAL,
        'unconnected-(U3H-VDD_PHY-PadP11)': si.NEUTRAL,
        'unconnected-(U3H-VDDQ_PHY-PadAB10)': si.NEUTRAL,
        'unconnected-(U3H-VDDQX_PHY-PadY1)': si.NEUTRAL,
        'unconnected-(U3H-VDDPLL_MCB_TOP_PHY-PadU12)': si.NEUTRAL,
        'unconnected-(U3H-VQPS-PadU7)': si.NEUTRAL,
        'unconnected-(U3J-GPIOB_N_32_CDI29-PadB4)': si.NEUTRAL,  # GPIO -- neutral IO
        'unconnected-(U3J-GPIOL_00_PLLIN1-PadV6)': si.AGGRESSOR,  # PLL input clock-ish
        'unconnected-(U3J-GPIOL_03_CLK24-PadW5)': si.AGGRESSOR,   # 24MHz clock out
        'unconnected-(U3J-GPIOL_04_CLK25-PadU2)': si.AGGRESSOR,
        'unconnected-(U3J-GPIOL_05_CLK26-PadT3)': si.AGGRESSOR,
        'unconnected-(U3J-GPIOL_06_CLK27-PadV1)': si.AGGRESSOR,
        'unconnected-(U3J-GPIOT_P_23_PLLIN0-PadD23)': si.AGGRESSOR,
        'unconnected-(U3J-GPIOL_21-PadV5)': si.NEUTRAL,
        'unconnected-(U3J-GPIOL_36_PLLIN1-PadU23)': si.AGGRESSOR,
        'unconnected-(U3J-GPIOL_40-PadW22)': si.NEUTRAL,
        'unconnected-(U3J-GPIOL_41-PadW23)': si.NEUTRAL,
        'unconnected-(U3J-GPIOL_42-PadW21)': si.NEUTRAL,
        'unconnected-(U3J-GPIOL_43-PadW19)': si.NEUTRAL,
        'unconnected-(U3J-GPIOL_44-PadW20)': si.NEUTRAL,
        'unconnected-(U3J-GPIOT_N_23-PadC23)': si.NEUTRAL,
        'unconnected-(U3J-GPIOT_N_33-PadG17)': si.NEUTRAL,
        'unconnected-(U3J-GPIOT_N_34-PadG18)': si.NEUTRAL,
        'unconnected-(U3J-GPIOT_P_33-PadF17)': si.NEUTRAL,
        'unconnected-(U3J-NC-PadV16)': si.NEUTRAL,
        'unconnected-(U3J-NC-PadW10)': si.NEUTRAL,
        'unconnected-(U3J-NC-PadW11)': si.NEUTRAL,
        'unconnected-(U3J-NC-PadW13)': si.NEUTRAL,
        'unconnected-(U3J-NC-PadW14)': si.NEUTRAL,
        'unconnected-(U3J-NC-PadW15)': si.NEUTRAL,
        '/fpga_adc/lvds_rx4_10_P': None,   # unlabeled (skip in precision)
    }


def test_board_precision():
    """Classify routed_output.kicad_pcb and report honest precision.

    We judge only the ROUTED nets (those carrying segments), against the hand
    labels above. Precision = correct / labeled. The purpose is honesty: name +
    metadata heuristics miss nets that carry no name signal (e.g. an opaque
    Net-() name whose pads carry no metadata), which is exactly why the override
    file exists.
    """
    print("board-level precision on routed_output.kicad_pcb (routed nets)")
    from kicad_parser import parse_kicad_pcb

    board = os.path.join(ROOT_DIR, 'kicad_files/routed_output.kicad_pcb')
    pcb = parse_kicad_pcb(board)
    res = si.classify_board(pcb, board_path=board)

    labels = _hand_labels()
    seg_nets = {}
    for s in pcb.segments:
        seg_nets[s.net_id] = True

    correct = 0
    total = 0
    misses = []
    for nid, info in res.items():
        if nid == -1:
            continue
        name = info['name']
        if name not in labels or labels[name] is None:
            continue
        if nid not in seg_nets:
            continue  # only judge routed nets
        total += 1
        if info['class'] == labels[name]:
            correct += 1
        else:
            misses.append((name, labels[name], info['class'], info['evidence']))

    precision = correct / total if total else 0.0
    print(f"  precision on labeled routed nets: {correct}/{total} = {precision:.3f}")
    for m in misses:
        print(f"  MISS {m[0]!r}: expected {m[1]}, got {m[2]} ({m[3]})")

    # The honest bar: we expect the classifier to be right on the clear cases.
    # Name heuristics are allowed to miss opaque nets (that's what the override
    # file is for), but it must not systematically mislabel the obvious ones.
    check(correct >= max(0.8 * total, total - 6),
          f"precision >= max(80%, total-6): {correct}/{total}",
          f"misses: {[m[0] for m in misses]}")


def test_d1_routed_nets():
    """Classify carrier_lab/d1.kicad_pcb's ROUTED nets and check the key ones.

    d1's 9 routed nets are the USB3 super-speed pairs (SSTX/SSRX) plus VBUS.
    The super-speed pairs are serial data -> VICTIM; VBUS is a switching supply
    branch -> AGGRESSOR. This pins the classifier against the board whose
    coupling the metric will actually measure.
    """
    print("board-level: carrier_lab/d1.kicad_pcb routed nets")
    from kicad_parser import parse_kicad_pcb

    board = os.path.join(ROOT_DIR, 'carrier_lab/d1.kicad_pcb')
    pcb = parse_kicad_pcb(board)
    res = si.classify_board(pcb, board_path=board)
    by_name = {info['name']: info['class'] for nid, info in res.items() if nid != -1}

    for name in ('SSTX1_P', 'SSTX1_N', 'SSTX2_P', 'SSTX2_N',
                 'SSRX1_P', 'SSRX1_N', 'SSRX2_P', 'SSRX2_N'):
        check(by_name.get(name) == si.VICTIM, f"d1 '{name}' -> VICTIM",
              f"got {by_name.get(name)}")
    check(by_name.get('VBUS') == si.AGGRESSOR, "d1 'VBUS' -> AGGRESSOR",
          f"got {by_name.get('VBUS')}")
    check(by_name.get('VBUS_IN') == si.AGGRESSOR, "d1 'VBUS_IN' -> AGGRESSOR",
          f"got {by_name.get('VBUS_IN')}")
    # GND must stay neutral/shield even though a power netclass claims it
    check(by_name.get('GND') == si.NEUTRAL, "d1 'GND' -> NEUTRAL (shield)",
          f"got {by_name.get('GND')}")


def main():
    test_name_patterns()
    test_priority()
    test_overrides()
    test_board_precision()
    test_d1_routed_nets()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
