"""si_classes.py -- signal-integrity net noise classifier.

Phase 1 of the signal-integrity-awareness initiative: classify every net on a
board into AGGRESSOR / VICTIM / NEUTRAL (SHIELD) so that coupling between
noisy and sensitive nets becomes measurable. Routing enforcement (aggressor-
proximity costs in the cost maps, planner corridor noise classes) is Phase 2
and deliberately OUT OF SCOPE here -- this module only CLASSIFIES.

The board owner's requirement (see carrier_lab/): the router must eventually
KNOW that sensitive lines (serial data, analog sense, radio) must not run
alongside noisy lines (switching power, clocks, motor drive). This phase builds
the classifier so violations become measurable before any routing enforcement
exists.

Classes:
  * AGGRESSOR -- noisy nets: switching-regulator nodes (SW/LX/BOOST/PHASE),
    PWM / motor / gate-drive, clocks, high-current supply branches.
  * VICTIM -- sensitive nets: UART/I2C/SPI/CAN RX-TX and other serial data,
    ADC/analog sense (AIN/VSENSE/thermocouple), radio/RF, reset/strap lines.
  * NEUTRAL -- everything else; solid ground/planes are SHIELDS (a separate
    concept, not a class -- see classify_board()).

Heuristics, in priority order (first match wins):
  1. Explicit per-board override file "<board>.si.json" (net -> class) always
     wins. See load_overrides().
  2. Connected-component hints: a net touching an inductor pad AND a diode/FET
     pad on a regulator footprint is a switch node -> AGGRESSOR. A net whose
     pads are ONLY on a regulator footprint and an inductor is a switch node.
  3. Netclass names (from the sibling .kicad_pro via list_nets.read_design_rules,
     or KiCad 6/7 (net_class ...) blocks): classes whose name matches the
     power/clock patterns below classify their member nets.
  4. Pad pinfunction / pintype metadata (where present): power_in/power_out
     on a switching-regulator footprint -> AGGRESSOR; input/output pins whose
     function names a serial/analog/radio signal -> VICTIM.
  5. Net-name patterns (the table below).
  6. Default: NEUTRAL.

The pattern table is documented inline so it can be extended without changing
the classification contract.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

AGGRESSOR = 'AGGRESSOR'
VICTIM = 'VICTIM'
NEUTRAL = 'NEUTRAL'
SHIELD = 'SHIELD'  # not a net class; a board-level concept for planes

# ---------------------------------------------------------------------------
# Pattern tables (documented)
# ---------------------------------------------------------------------------

# Net-name patterns. Each entry is (compiled_regex, class). The table is
# evaluated in order; the first match wins. Patterns are applied to the net
# name with the leading '/' stripped and case-folded.
#
# AGGRESSOR patterns -- noisy nets:
#   * Switching-regulator nodes: SW, LX, BOOST, PHASE, BST (bootstrap), and the
#     generic "switcher output" tokens. These carry hard-edged switching edges.
#   * PWM / motor / gate-drive: PWM, MOTOR, GATE, DRV (gate drive), HDRV / LDRV,
#     PHASE/U/V/W (motor phases), ENBL/EN (enable is control, not noise, but an
#     aggressive enable line is rare -- keep it AGGRESSOR for safety).
#   * Clocks: CLK, OSC, PLL (reference clocks), XTAL (crystal drive pins).
#   * High-current supply branches: VBULK/VIN/VOUT/VBUS/VLED/VDRV/VSW + a digit
#     or suffix where the net carries switching current. We are deliberately
#     conservative here: plain +3V3/+5V rails are NEUTRAL (they are quiet DC);
#     only names that clearly suggest a switching node or a high-current branch
#     land in AGGRESSOR.
#
# VICTIM patterns -- sensitive nets:
#   * Serial data: UART/RX/TX/SDA/SCL/SCK/MOSI/MISO/SPI/I2C/CAN/RX/TX, plus the
#     bidirectional *_D / *_CLK of SDIO and USB D+/D-.
#   * Analog sense: AIN/ADC/VSENSE/SENSE/THERMO/TC/RTD/AMP/REF(erence)/VREF,
#     and *_AN/_AP thermocouple pairs.
#   * Radio / RF: RF/RADIO/ANT(enna)/LNA/MIX/IF/BB (baseband), BT/BLE/WIFI.
#   * Reset / strap: RESET/RST/nRST/nCS/CS (chip select)/EN? No -- EN is an
#     enable, keep it AGGRESSOR; RESET and strap lines are VICTIM because a
#     glitch on them resets or misstraps the system.
#
# The escape hatch for any net the table gets wrong is the per-board override
# file (<board>.si.json) -- see load_overrides().
NAME_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # -- AGGRESSOR --
    # switching-regulator switch nodes
    (re.compile(r'(^|[^a-z0-9])(sw|lx|boost|phase|bst|switch|switcher)([^a-z0-9]|$)'), AGGRESSOR),
    # PWM / motor / gate drive
    (re.compile(r'(^|[^a-z0-9])(pwm|motor|gate|hdrv|ldrv|drv)([^a-z0-9]|$)'), AGGRESSOR),
    # clocks / oscillators / PLL (incl. clkin/clkout -- lvds_rx_top_clkin1_P)
    (re.compile(r'(^|[^a-z0-9])(clk|osc|pll|xtal|crystal)([^a-z0-9]|$)'), AGGRESSOR),
    (re.compile(r'(^|[^a-z0-9])(clkin|clkout)([0-9]*)([^a-z0-9]|$)'), AGGRESSOR),
    # DDR clock/data-strobe pins (CK_t/CK_c, DQS) -- routed_output's DDR bus
    # nets are Net-(U1B-CK_t_A) / Net-(U1A-DQS0_t_A); tolerate trailing digits
    # and underscores after the token.
    (re.compile(r'(^|[^a-z0-9])(ck|dqs)([0-9]*)([_]|$)'), AGGRESSOR),
    # high-current supply branches (switching current): VBULK, VIN_RAW, VOUT_PD,
    # VBUS_IN, VLED, VDRV, VSW...
    (re.compile(r'(^|[^a-z0-9])(vbulk|vbus|vled|vdrv|vsw|vin|vout)([^a-z0-9_]?|$)'), AGGRESSOR),
    # -- VICTIM --
    # serial data: UART / I2C / SPI / CAN / SMBus / I2S / SDIO / USB data.
    # Protocol tokens tolerate a trailing bus index (SPI0_, UART2_, CAN1_) and
    # data tokens tolerate a trailing lane index (SDA1, SSRX2_P) -- the
    # [0-9]* between the keyword and the terminator handles both.
    (re.compile(r'(^|[^a-z0-9])(uart|i2c|spi|can|smbus|i2s|sdio)([0-9]*)([^a-z0-9]|$)'), VICTIM),
    # sclk is the SPI clock (VICTIM -- a serial clock, not a free-running
    # noise clock); sck is its alias. sda/scl are I2C data/clock.
    (re.compile(r'(^|[^a-z0-9])(sclk)([0-9]*)([^a-z0-9]|$)'), VICTIM),
    (re.compile(r'(^|[^a-z0-9])(sda|scl|sck|mosi|miso)([0-9]*)([^a-z0-9]|$)'), VICTIM),
    (re.compile(r'(^|[^a-z0-9])(rx|tx)([0-9]*)([^a-z0-9]|$)'), VICTIM),
    (re.compile(r'(^|[^a-z0-9])usb([0-9]*)([^a-z0-9]|$)'), VICTIM),
    (re.compile(r'(^|[^a-z0-9])(dm|dp)([^a-z0-9]|$)'), VICTIM),
    # USB3 super-speed pairs -- d1's SSTX*/SSRX* are its routed nets
    (re.compile(r'(^|[^a-z0-9])(sstx|ssrx)([0-9]*)([^a-z0-9]|$)'), VICTIM),
    # DDR data / address / strobe pins and FX2 FIFO data pins -- parallel
    # digital data is still a victim when it runs alongside switching power.
    # Tokens tolerate trailing digits/underscores: DQ0_A, DQS0_t_A, CA0_A,
    # DATA_0.
    (re.compile(r'(^|[^a-z0-9])(dq|dm|dqs|ca)([0-9]*)([^a-z0-9]|$)'), VICTIM),
    # FX2 FIFO data pins (routed_output): DATA_0..DATA_31
    (re.compile(r'(^|[^a-z0-9])data_([0-9]+)([^a-z0-9]|$)'), VICTIM),
    # analog sense: ADC / AIN / sense / thermo / thermocouple / TC / RTD / amp / ref
    (re.compile(r'(^|[^a-z0-9])(adc|ain|sense|thermo|thermocouple|rtd|amp|ref)([0-9]*)([^a-z0-9]|$)'), VICTIM),
    # thermocouple channels -- d1's TC1_AN/TC1_AP pairs feed the ADS124S08
    (re.compile(r'(^|[^a-z0-9])tc([0-9]*)([^a-z0-9]|$)'), VICTIM),
    # radio / RF
    (re.compile(r'(^|[^a-z0-9])(rf|radio|ant|lna|mixer|if_|bb|bt_|ble|wifi)([^a-z0-9]|$)'), VICTIM),
    # reset / strap lines
    (re.compile(r'(^|[^a-z0-9])(reset|rst)([^a-z0-9]|$)'), VICTIM),
]

# Netclass-name patterns. Class names are matched the same way as net names.
# A netclass whose name matches a power/switching pattern classifies its member
# nets as AGGRESSOR; one matching a serial/analog pattern classifies them as
# VICTIM. Applied before pad metadata and net names (netclass membership is an
# explicit designer statement).
NETCLASS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'(^|[^a-z0-9])(sw|lx|boost|phase|pwm|motor|gate|drv|clk|osc|pll)([^a-z0-9]|$)'), AGGRESSOR),
    (re.compile(r'(^|[^a-z0-9])(power|pwr)([^a-z0-9]?|$)'), AGGRESSOR),
    (re.compile(r'(^|[^a-z0-9])(uart|i2c|spi|can|smbus|i2s|sdio|usb)([^a-z0-9]|$)'), VICTIM),
    (re.compile(r'(^|[^a-z0-9])(adc|ain|sense|analog|radio|rf)([^a-z0-9]|$)'), VICTIM),
]

# Pinfunction-name patterns for pad metadata (routed_output / rp2350 carry
# pinfunction + pintype; d1/orangecrab do not). A pad whose pinfunction names a
# serial/analog/radio signal classifies its net as VICTIM; one that names a
# switching/power signal on a regulator footprint classifies it as AGGRESSOR.
# Pinfunction tokens that mark a net VICTIM (serial/analog/radio) -- used
# AFTER the aggressor check so a clock lane with 'rx' in its name is still
# an aggressor. DDR data/address pins are serial-ish victims.
PIN_VICTIM_PATTERNS: List[re.Pattern] = [
    re.compile(r'(^|[^a-z0-9])(uart|i2c|spi|can|smbus|i2s|sdio|sda|scl|sclk|sck|mosi|miso|rx|tx)([^a-z0-9]|$)', re.I),
    re.compile(r'(^|[^a-z0-9])(adc|ain|sense|thermo|rtd)([^a-z0-9]|$)', re.I),
    re.compile(r'(^|[^a-z0-9])(rf|radio|lna)([^a-z0-9]|$)', re.I),
    re.compile(r'(^|[^a-z0-9])(reset|rst)([^a-z0-9]|$)', re.I),
    # DDR data / address / strobe pins and FX2 FIFO data pins -- parallel
    # digital data is still a victim when it runs alongside switching power.
    re.compile(r'(^|[^a-z0-9])(dq|dm|dqs|ca)([0-9]*)([^a-z0-9]|$)', re.I),
    # FX2 FIFO data pins (routed_output): DATA_0..DATA_31, ftdi_data[n]
    re.compile(r'(data_[0-9]+|ftdi_data)', re.I),
]

# Pinfunction tokens that mark a net AGGRESSOR (switching nodes / clocks /
# PWM) -- checked BEFORE the victim tokens so a clock lane whose function also
# contains 'rx' (e.g. lvds_rx_top_clkin1) stays an aggressor.
PIN_AGGRESSOR_PATTERNS: List[re.Pattern] = [
    re.compile(r'(^|[^a-z0-9])(sw|lx|boost|phase|bst)([^a-z0-9]|$)', re.I),
    re.compile(r'(^|[^a-z0-9])(pwm|motor|gate|hdrv|ldrv)([^a-z0-9]|$)', re.I),
    # clk/clkin/clkout tolerate trailing digits (lvds_rx_top_clkin1)
    re.compile(r'(^|[^a-z0-9])(clk|clkin[0-9]*|clkout[0-9]*|osc|pll|xtal|crystal)([^a-z0-9]|$)', re.I),
    re.compile(r'(^|[^a-z0-9])(ck_|ck$|dqs)([^a-z0-9]|$)', re.I),
]

# Regulator/switching footprints: if a pad's pintype is power_in/power_out AND
# the footprint value/name suggests a switching regulator, the net is an
# AGGRESSOR supply branch. This catches the LM5175/TPS54560/AP63203 switch-node
# nets on d1 that the name table misses.
REGULATOR_HINT = re.compile(
    r'(lm[0-9]+|tps[0-9]+|ap[0-9]+|mp[0-9]+|buck|boost|switcher|regulator)', re.I)

# ---------------------------------------------------------------------------
# Override file support
# ---------------------------------------------------------------------------

def override_path(board_path: str) -> str:
    """Return the per-board override file path: <board>.si.json.

    E.g. for 'carrier_lab/d1.kicad_pcb' -> 'carrier_lab/d1.si.json'.
    """
    root, _ = os.path.splitext(board_path)
    return root + '.si.json'


def load_overrides(board_path: str) -> Dict[str, str]:
    """Load the per-board override file (<board>.si.json), if present.

    The file is a JSON object mapping net name -> class ('AGGRESSOR' |
    'VICTIM' | 'NEUTRAL'). It ALWAYS wins over every heuristic. Missing file ->
    empty dict. Malformed JSON -> empty dict (never crash classification).
    """
    path = override_path(board_path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            vn = str(v).upper()
            if vn in (AGGRESSOR, VICTIM, NEUTRAL):
                out[str(k)] = vn
        return out
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Connected-component hints (switch-node detection)
# ---------------------------------------------------------------------------

def _is_inductor_footprint(f) -> bool:
    fn = (f.footprint_name or '').lower()
    # A ferrite bead (BLM/FBM/ferrite) is NOT a switch-node inductor -- it is a
    # quiet filter element; counting it would turn output rails into phantom
    # switch nodes (d1's +3V3/+5V touched FB1/FB2 + a regulator).
    if 'ferrite' in fn or 'bead' in fn or fn.startswith('fb'):
        return False
    return 'inductor' in fn or fn.startswith('l_')


def _is_diode_footprint(f) -> bool:
    fn = (f.footprint_name or '').lower()
    return 'diode' in fn or fn.startswith('d_')


def _is_fet_footprint(f) -> bool:
    # Match only ACTUAL power-FET footprints. The old 'son-'/'vson' substring
    # matched every WSON/QFN package (orangecrab's TPS51206 and flash chip were
    # flagged as FETs), which turned quiet supply rails into phantom switch
    # nodes. Real FET footprints carry mosfet/nexfet/powerpa/lfpak/so-8fl or a
    # bare 'fet' token (d1's CSD18543Q3A lives in
    # Package_SON:VSON-8_..._NexFET).
    fn = (f.footprint_name or '').lower()
    return bool(re.search(r'(mosfet|nexfet|powerpa|lfpak|so-8fl|\bfet\b)', fn))


def switch_node_hints(pcb) -> Dict[int, str]:
    """Return {net_id: reason} for nets that look like switching-regulator
    switch nodes from connected-component evidence.

    A net is a switch node if it touches BOTH an inductor pad AND a diode/FET
    pad on a switching-regulator footprint. This is the strongest structural
    signal available on boards whose pads carry no pin metadata (d1,
    orangecrab): the LM5175 buck-boost on d1 has Net-(Q3-D) touching inductor
    L2 and FETs Q2/Q3/Q4, and Net-(U5-SW) touching inductor L5 and the AP63203
    regulator.

    Also catches a net whose pads sit ONLY on a regulator footprint AND an
    inductor (e.g. Net-(U5-SW) on the TSOT AP63203 + L5).
    """
    out: Dict[int, str] = {}
    touches: Dict[int, set] = defaultdict(set)
    for f in pcb.footprints.values():
        kind = set()
        if _is_inductor_footprint(f):
            kind.add('inductor')
        if _is_diode_footprint(f):
            kind.add('diode')
        if _is_fet_footprint(f):
            kind.add('fet')
        if REGULATOR_HINT.search((f.value or '') + ' ' + (f.footprint_name or '')):
            kind.add('regulator')
        if not kind:
            continue
        for p in f.pads:
            if p.net_id > 0:
                touches[p.net_id].update(kind)

    for nid, kinds in touches.items():
        if 'inductor' in kinds and ('diode' in kinds or 'fet' in kinds):
            out[nid] = 'switch node: inductor + diode/FET'
    return out


def classify_net(net_name: str,
                 netclass: str = '',
                 pinfunctions: Optional[List[str]] = None,
                 pintypes: Optional[List[str]] = None,
                 on_regulator_footprint: bool = False,
                 switch_hint: bool = False,
                 override: Optional[str] = None) -> str:
    """Classify ONE net from its evidence.

    Args:
        net_name: the net's name (leading '/' tolerated).
        netclass: the net's KiCad netclass name, if any.
        pinfunctions/pintypes: pinfunction/pintype strings of the pads on this
            net (empty list if the board carries no metadata).
        on_regulator_footprint: True if any pad sits on a switching-regulator
            footprint.
        switch_hint: True if connected-component analysis flagged this net as a
            switch node.
        override: explicit classification from the per-board override file.

    Priority: override > switch_hint > netclass > pin metadata > net name >
    NEUTRAL.
    """
    if override:
        return override

    if switch_hint:
        return AGGRESSOR

    if netclass:
        nc = netclass.strip().lower()
        for pat, cls in NETCLASS_PATTERNS:
            if pat.search(nc):
                return cls

    pinfuncs = pinfunctions or []
    pintypes_l = pintypes or []
    is_power_pin = any(t in ('power_in', 'power_out') for t in pintypes_l)
    for pf in pinfuncs:
        if is_power_pin:
            # A power pin's function may mention a domain (ADC_AVDD, VCCA_PLL)
            # without making the RAIL a signal -- only a switching/power token
            # on a regulator footprint turns the net into an AGGRESSOR branch;
            # otherwise power pins never make a net VICTIM.
            if on_regulator_footprint:
                for pat in PIN_AGGRESSOR_PATTERNS:
                    if pat.search(pf):
                        return AGGRESSOR
            continue
        # AGGRESSOR tokens win over VICTIM tokens inside a single pin function:
        # an LVDS receiver CLOCK input ('lvds_rx_top_clkin1') carries both 'rx'
        # and 'clkin' -- it is a clock lane feeding an ADC, i.e. an aggressor,
        # not a serial-data victim.
        for pat in PIN_AGGRESSOR_PATTERNS:
            if pat.search(pf):
                return AGGRESSOR
        for pat in PIN_VICTIM_PATTERNS:
            if pat.search(pf):
                return VICTIM

    if net_name:
        name = net_name.lstrip('/').lower()
        for pat, cls in NAME_PATTERNS:
            if pat.search(name):
                return cls

    return NEUTRAL


def classify_board(pcb,
                   board_path: Optional[str] = None,
                   design_rules=None,
                   use_overrides: bool = True) -> Dict[int, dict]:
    """Classify every net on a parsed board.

    Returns {net_id(int): {name, class, evidence}} for every net with pads or
    segments. board_path is used to locate the per-board override file; pass it
    when available. design_rules is the dict from list_nets.read_design_rules()
    (netclass membership); it is resolved lazily from board_path when omitted.

    The result also carries the SHIELD set under the special key -1:
      {-1: {'name': '<shields>', 'class': 'SHIELD', 'evidence': [...]}}
    listing nets whose name matches a solid-ground/plane pattern (GND*, ground,
    plane nets). SHIELD is not one of the three classes -- it is a board-level
    concept used by the coupling metric to decide which copper layer between a
    victim and an aggressor actually shields them.
    """
    overrides = load_overrides(board_path) if (use_overrides and board_path) else {}

    # Resolve netclass membership.
    assignments: Dict[str, str] = {}
    patterns: List[Tuple[str, str]] = []
    if design_rules is None and board_path:
        try:
            import list_nets  # local import to keep si_classes importable standalone
            design_rules = list_nets.read_design_rules(board_path)
        except Exception:
            design_rules = None
    if design_rules:
        assignments = design_rules.get('assignments') or {}
        patterns = design_rules.get('patterns') or []

    def netclass_for(name: str) -> str:
        if name in assignments:
            return assignments[name]
        for pat, cls in patterns:
            try:
                if fnmatch.fnmatch(name, pat):
                    return cls
            except Exception:
                continue
        return ''

    result: Dict[int, dict] = {}
    shields: List[str] = []

    switch_hints = switch_node_hints(pcb)

    for nid, net in pcb.nets.items():
        name = net.name or ''
        pads = list(net.pads)

        pinfuncs = [p.pinfunction for p in pads if p.pinfunction]
        pintypes_l = [p.pintype for p in pads if p.pintype]
        on_reg = any(
            REGULATOR_HINT.search((pcb.footprints.get(p.component_ref).value or '')
                                  + ' ' + (pcb.footprints.get(p.component_ref).footprint_name or ''))
            for p in pads if p.component_ref in pcb.footprints)

        override = overrides.get(name)
        nc = netclass_for(name)
        cls = classify_net(
            name,
            netclass=nc,
            pinfunctions=pinfuncs,
            pintypes=pintypes_l,
            on_regulator_footprint=on_reg,
            switch_hint=nid in switch_hints,
            override=override,
        )

        # A solid ground net is a SHIELD, never an aggressor -- even when a
        # power netclass pattern (e.g. 'PWR_MED' on d1's GND) claims it. The
        # coupling metric treats GND copper as quiet reference, not noise.
        nlow = name.lstrip('/').lower()
        is_shield = nlow.startswith('gnd') or nlow in ('ground',)
        if is_shield:
            cls = NEUTRAL
            shields.append(name)

        evidence = []
        if override:
            evidence.append("override file")
        elif nid in switch_hints:
            evidence.append(switch_hints[nid])
        else:
            if nc:
                evidence.append(f"netclass '{nc}'")
            if pinfuncs:
                evidence.append("pin metadata")
            evidence.append("name pattern" if cls != NEUTRAL else "default")
        if is_shield:
            evidence.append("shield (ground)")

        result[nid] = {'name': name, 'class': cls, 'evidence': evidence}

    result[-1] = {'name': '<shields>', 'class': SHIELD,
                  'evidence': shields}
    return result


def summary(result: Dict[int, dict]) -> Dict[str, int]:
    """Count nets per class from a classify_board() result."""
    counts = {AGGRESSOR: 0, VICTIM: 0, NEUTRAL: 0}
    for nid, info in result.items():
        if nid == -1:
            continue
        cls = info['class']
        if cls in counts:
            counts[cls] += 1
    counts['SHIELD'] = len(result.get(-1, {}).get('evidence', []))
    return counts


if __name__ == '__main__':
    import sys
    for b in sys.argv[1:] or ['carrier_lab/d1.kicad_pcb']:
        from kicad_parser import parse_kicad_pcb  # noqa: E402
        pcb = parse_kicad_pcb(b)
        res = classify_board(pcb, board_path=b)
        print(f"== {b}")
        print("  counts:", summary(res))
        for nid, info in sorted(res.items()):
            if nid == -1:
                continue
            print(f"  {info['class']:<10} {info['name']:<40} {info['evidence']}")
