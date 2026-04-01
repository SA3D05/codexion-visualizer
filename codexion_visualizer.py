#!/usr/bin/env python3
"""
Codexion log visualizer.

Improved features:
- person icons instead of plain circles
- animated left/right dongle pickup using richer log lines
- dongle colors: available / taken / cooldown
- scrollable logs panel
- current simulated time in corner
- event mode and smooth interpolation between events
- cleaner UI with better panels, shadows, badges, and timeline

Supported log lines:
    0 1 has taken a dongle
    384 4 has taken a left 4 dongle
    384 4 has taken a right 1 dongle
    1 1 is compiling
    201 1 is debugging
    401 1 is refactoring
    1204 3 burned out

Controls:
    ENTER      toggle step/auto
    SPACE      next event in step mode / pause in auto
    E          toggle event mode
    RIGHT      next event
    LEFT       previous event
    SHIFT+UP   bigger gap between events in event mode
    SHIFT+DOWN smaller gap between events in event mode
    UP         faster
    DOWN       slower
    W/S        scroll logs up/down
    PAGEUP/DN  scroll logs faster
    R          restart
    G          jump to last event
    H          help
    T          toggle timestamps in log view
    ESC/Q      quit

Examples:
    python3 codexion_visualizer.py --run ./codex 4 45 10 10 10 10 10 edf
    ./codex 4 45 10 10 10 10 10 edf | python3 codexion_visualizer.py --stdin --coders 4

Install:
    python3 -m pip install pygame
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import pygame
except ImportError as exc:
    raise SystemExit(
        "pygame is required. Install it with: python3 -m pip install pygame"
    ) from exc


LOG_RE = re.compile(r"^(\d+)\s+(\d+)\s+(.+?)\s*$")
PICK_DETAILED_RE = re.compile(r"^has taken a (left|right) (\d+) dongle$")
PICK_GENERIC_RE = re.compile(r"^has taken a dongle$")

STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_COMPILING = "compiling"
STATE_DEBUGGING = "debugging"
STATE_REFACTORING = "refactoring"
STATE_BURNED = "burned"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


@dataclass
class Event:
    time_ms: int
    coder_id: int
    action: str
    raw: str
    index: int
    pickup_side: Optional[str] = None
    dongle_id: Optional[int] = None


@dataclass
class CoderState:
    coder_id: int
    state: str = STATE_IDLE
    compile_count: int = 0
    dongles_held: int = 0
    left_dongle_id: Optional[int] = None
    right_dongle_id: Optional[int] = None
    last_action: str = ""
    last_event_time: int = 0
    burned: bool = False
    waiting_since: Optional[int] = None


@dataclass
class DongleState:
    dongle_id: int
    status: str = "available"  # available / taken / cooldown
    holder: Optional[int] = None
    cooldown_until: int = 0


@dataclass
class Config:
    number_of_coders: int
    time_to_burnout: Optional[int] = None
    time_to_compile: Optional[int] = None
    time_to_debug: Optional[int] = None
    time_to_refactor: Optional[int] = None
    number_of_compiles_required: Optional[int] = None
    dongle_cooldown: Optional[int] = None
    scheduler: Optional[str] = None


@dataclass
class Snapshot:
    current_time_ms: int
    next_event_index: int
    coders: Dict[int, CoderState] = field(default_factory=dict)
    dongles: Dict[int, DongleState] = field(default_factory=dict)
    recent_events: List[Event] = field(default_factory=list)


class LogParser:
    @staticmethod
    def parse_lines(lines: List[str]) -> List[Event]:
        events: List[Event] = []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            m = LOG_RE.match(line)
            if not m:
                continue
            time_ms = int(m.group(1))
            coder_id = int(m.group(2))
            action = m.group(3)
            pickup_side = None
            dongle_id = None
            md = PICK_DETAILED_RE.match(action)
            if md:
                pickup_side = md.group(1)
                dongle_id = int(md.group(2))
            events.append(
                Event(
                    time_ms=time_ms,
                    coder_id=coder_id,
                    action=action,
                    raw=line,
                    index=len(events),
                    pickup_side=pickup_side,
                    dongle_id=dongle_id,
                )
            )
        events.sort(key=lambda e: (e.time_ms, e.index))
        return events


class Simulator:
    def __init__(self, config: Config, events: List[Event]):
        self.config = config
        self.events = events
        self.snapshots: List[Snapshot] = []
        self._build_snapshots()

    def _new_coders(self) -> Dict[int, CoderState]:
        return {
            i: CoderState(coder_id=i)
            for i in range(1, self.config.number_of_coders + 1)
        }

    def _new_dongles(self) -> Dict[int, DongleState]:
        return {
            i: DongleState(dongle_id=i)
            for i in range(1, self.config.number_of_coders + 1)
        }

    def _clone_coders(self, coders: Dict[int, CoderState]) -> Dict[int, CoderState]:
        return {
            i: CoderState(
                coder_id=c.coder_id,
                state=c.state,
                compile_count=c.compile_count,
                dongles_held=c.dongles_held,
                left_dongle_id=c.left_dongle_id,
                right_dongle_id=c.right_dongle_id,
                last_action=c.last_action,
                last_event_time=c.last_event_time,
                burned=c.burned,
                waiting_since=c.waiting_since,
            )
            for i, c in coders.items()
        }

    def _clone_dongles(self, dongles: Dict[int, DongleState]) -> Dict[int, DongleState]:
        return {
            i: DongleState(
                dongle_id=d.dongle_id,
                status=d.status,
                holder=d.holder,
                cooldown_until=d.cooldown_until,
            )
            for i, d in dongles.items()
        }

    def _update_cooldowns(self, dongles: Dict[int, DongleState], now_ms: int) -> None:
        for d in dongles.values():
            if d.status == "cooldown" and now_ms >= d.cooldown_until:
                d.status = "available"
                d.holder = None

    def _release_coder_dongles(
        self,
        coders: Dict[int, CoderState],
        dongles: Dict[int, DongleState],
        coder: CoderState,
        now_ms: int,
    ) -> None:
        for did in (coder.left_dongle_id, coder.right_dongle_id):
            if did is None or did not in dongles:
                continue
            d = dongles[did]
            d.status = "cooldown"
            d.holder = None
            d.cooldown_until = now_ms + (self.config.dongle_cooldown or 0)
        coder.left_dongle_id = None
        coder.right_dongle_id = None
        coder.dongles_held = 0

    def _apply(
        self,
        coders: Dict[int, CoderState],
        dongles: Dict[int, DongleState],
        event: Event,
    ) -> None:
        self._update_cooldowns(dongles, event.time_ms)
        coder = coders[event.coder_id]
        coder.last_action = event.action
        coder.last_event_time = event.time_ms

        if event.pickup_side and event.dongle_id is not None:
            d = dongles.get(event.dongle_id)
            if d:
                d.status = "taken"
                d.holder = coder.coder_id
            coder.dongles_held = min(2, coder.dongles_held + 1)
            if event.pickup_side == "left":
                coder.left_dongle_id = event.dongle_id
            else:
                coder.right_dongle_id = event.dongle_id
            if coder.state not in (STATE_COMPILING, STATE_BURNED):
                coder.state = STATE_WAITING
                if coder.waiting_since is None:
                    coder.waiting_since = event.time_ms
            return

        if PICK_GENERIC_RE.match(event.action):
            coder.dongles_held = min(2, coder.dongles_held + 1)
            if coder.state not in (STATE_COMPILING, STATE_BURNED):
                coder.state = STATE_WAITING
                if coder.waiting_since is None:
                    coder.waiting_since = event.time_ms
            return

        if event.action == "is compiling":
            coder.state = STATE_COMPILING
            coder.compile_count += 1
            coder.waiting_since = None
        elif event.action == "is debugging":
            coder.state = STATE_DEBUGGING
            self._release_coder_dongles(coders, dongles, coder, event.time_ms)
        elif event.action == "is refactoring":
            coder.state = STATE_REFACTORING
        elif event.action == "burned out":
            coder.state = STATE_BURNED
            coder.burned = True
            self._release_coder_dongles(coders, dongles, coder, event.time_ms)

    def _build_snapshots(self) -> None:
        coders = self._new_coders()
        dongles = self._new_dongles()
        recent: List[Event] = []
        self.snapshots = [
            Snapshot(
                current_time_ms=0,
                next_event_index=0,
                coders=self._clone_coders(coders),
                dongles=self._clone_dongles(dongles),
                recent_events=[],
            )
        ]
        for idx, event in enumerate(self.events):
            self._apply(coders, dongles, event)
            recent.append(event)
            recent = recent[-100:]
            self.snapshots.append(
                Snapshot(
                    current_time_ms=event.time_ms,
                    next_event_index=idx + 1,
                    coders=self._clone_coders(coders),
                    dongles=self._clone_dongles(dongles),
                    recent_events=list(recent),
                )
            )

    def get_snapshot(self, event_pointer: int) -> Snapshot:
        pointer = int(clamp(event_pointer, 0, len(self.snapshots) - 1))
        return self.snapshots[pointer]


class Visualizer:
    WIDTH = 1920
    HEIGHT = 1080
    SIDE_W = 430
    BOTTOM_H = 270
    FPS = 60

    BG = (10, 14, 22)
    BG2 = (19, 26, 38)
    PANEL = (17, 24, 35)
    PANEL2 = (25, 33, 48)
    SHADOW = (0, 0, 0, 70)
    TEXT = (236, 240, 246)
    MUTED = (162, 173, 190)
    GRID = (55, 68, 88)
    ACCENT = (85, 164, 255)
    ACCENT2 = (126, 95, 255)
    GOOD = (61, 201, 134)
    WARN = (255, 179, 71)
    DANGER = (245, 92, 92)
    DONGLE_AVAIL = (64, 210, 132)
    DONGLE_TAKEN = (84, 163, 255)
    DONGLE_COOLDOWN = (255, 170, 77)
    TABLE = (117, 73, 47)
    TABLE_EDGE = (161, 105, 73)

    def __init__(self, simulator: Simulator, ms_to_seconds: float, step_mode: bool):
        self.sim = simulator
        self.ms_to_seconds = ms_to_seconds
        self.step_mode = step_mode
        self.event_mode = False
        self.event_gap_seconds = 0.35
        self.show_help = True
        self.show_timestamps = True
        self.paused = step_mode
        self.pointer = 0
        self.playhead_ms = 0.0
        self.event_gap_progress = 0.0
        self.logs_scroll = 0
        self.clock = None
        self.screen = None
        self.font = None
        self.small = None
        self.tiny = None
        self.big = None
        self.max_time = simulator.events[-1].time_ms if simulator.events else 0
        self.center = (
            (self.WIDTH - self.SIDE_W) // 2,
            (self.HEIGHT - self.BOTTOM_H) // 2 + 20,
        )
        self.table_radius = min(self.center[0], self.center[1]) * 0.48
        self.person_scale = 1.0
        self.anim_duration = 0.55

    def run(self) -> None:
        pygame.init()
        pygame.display.set_caption("Codexion Visualizer")
        self.screen = pygame.display.set_mode((self.WIDTH, self.HEIGHT))
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("DejaVu Sans", 22)
        self.small = pygame.font.SysFont("DejaVu Sans", 18)
        self.tiny = pygame.font.SysFont("DejaVu Sans", 15)
        self.big = pygame.font.SysFont("DejaVu Sans", 30, bold=True)

        running = True
        while running:
            dt = self.clock.tick(self.FPS) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN:
                    running = self._handle_keydown(ev.key, pygame.key.get_mods())
                    if not running:
                        break
                elif ev.type == pygame.MOUSEWHEEL:
                    self.logs_scroll = max(0, self.logs_scroll - ev.y)

            self._update(dt)
            self._draw()
            pygame.display.flip()

        pygame.quit()

    def _handle_keydown(self, key: int, mods: int) -> bool:
        if key in (pygame.K_ESCAPE, pygame.K_q):
            return False
        if key == pygame.K_h:
            self.show_help = not self.show_help
        elif key == pygame.K_t:
            self.show_timestamps = not self.show_timestamps
        elif key == pygame.K_e:
            self.event_mode = not self.event_mode
            self.paused = self.step_mode
            self.event_gap_progress = 0.0
        elif key == pygame.K_RETURN:
            self.step_mode = not self.step_mode
            self.paused = self.step_mode
        elif key == pygame.K_SPACE:
            if self.step_mode:
                self._step_forward()
            else:
                self.paused = not self.paused
        elif key == pygame.K_RIGHT:
            self._step_forward()
        elif key == pygame.K_LEFT:
            self._step_backward()
        elif key == pygame.K_r:
            self.pointer = 0
            self.playhead_ms = 0.0
            self.event_gap_progress = 0.0
            self.logs_scroll = 0
            self.paused = self.step_mode
        elif key == pygame.K_g:
            self.pointer = len(self.sim.snapshots) - 1
            self.playhead_ms = float(
                self.sim.get_snapshot(self.pointer).current_time_ms
            )
            self.event_gap_progress = 0.0
            self.paused = True
        elif key == pygame.K_UP and not (mods & pygame.KMOD_SHIFT):
            self.ms_to_seconds = min(self.ms_to_seconds * 2.0, 10.0)
        elif key == pygame.K_DOWN and not (mods & pygame.KMOD_SHIFT):
            self.ms_to_seconds = max(self.ms_to_seconds / 2.0, 0.001)
        elif key == pygame.K_UP and (mods & pygame.KMOD_SHIFT):
            self.event_gap_seconds = min(2.0, self.event_gap_seconds + 0.05)
        elif key == pygame.K_DOWN and (mods & pygame.KMOD_SHIFT):
            self.event_gap_seconds = max(0.02, self.event_gap_seconds - 0.05)
        elif key == pygame.K_w:
            self.logs_scroll = max(0, self.logs_scroll - 1)
        elif key == pygame.K_s:
            self.logs_scroll += 1
        elif key == pygame.K_PAGEUP:
            self.logs_scroll = max(0, self.logs_scroll - 8)
        elif key == pygame.K_PAGEDOWN:
            self.logs_scroll += 8
        return True

    def _step_forward(self) -> None:
        if self.pointer < len(self.sim.snapshots) - 1:
            self.pointer += 1
            self.playhead_ms = float(
                self.sim.get_snapshot(self.pointer).current_time_ms
            )
            self.event_gap_progress = 0.0

    def _step_backward(self) -> None:
        if self.pointer > 0:
            self.pointer -= 1
            self.playhead_ms = float(
                self.sim.get_snapshot(self.pointer).current_time_ms
            )
            self.event_gap_progress = 0.0

    def _update(self, dt_seconds: float) -> None:
        if self.step_mode or self.paused or not self.sim.events:
            return

        if self.event_mode:
            if self.pointer < len(self.sim.snapshots) - 1:
                self.event_gap_progress += dt_seconds
                if self.event_gap_progress >= self.event_gap_seconds:
                    self.pointer += 1
                    self.event_gap_progress = 0.0
                    self.playhead_ms = float(
                        self.sim.get_snapshot(self.pointer).current_time_ms
                    )
            else:
                self.paused = True
            return

        ms_per_real_second = 1.0 / self.ms_to_seconds
        self.playhead_ms += dt_seconds * ms_per_real_second
        while self.pointer < len(self.sim.events):
            next_time = self.sim.events[self.pointer].time_ms
            if self.playhead_ms + 1e-9 >= next_time:
                self.pointer += 1
            else:
                break
        if self.pointer >= len(self.sim.snapshots) - 1:
            self.pointer = len(self.sim.snapshots) - 1
            self.paused = True

    def _get_transition(self) -> Tuple[Snapshot, Snapshot, float]:
        curr_idx = int(clamp(self.pointer, 0, len(self.sim.snapshots) - 1))
        curr_snap = self.sim.get_snapshot(curr_idx)
        next_snap = self.sim.get_snapshot(
            min(curr_idx + 1, len(self.sim.snapshots) - 1)
        )

        if self.step_mode or self.paused:
            return curr_snap, next_snap, 0.0

        if self.event_mode:
            t = clamp(
                self.event_gap_progress / max(self.event_gap_seconds, 0.001), 0.0, 1.0
            )
            return curr_snap, next_snap, t

        if curr_idx >= len(self.sim.events):
            return curr_snap, next_snap, 0.0

        curr_time = curr_snap.current_time_ms
        next_time = next_snap.current_time_ms
        if next_time <= curr_time:
            return curr_snap, next_snap, 0.0
        t = clamp((self.playhead_ms - curr_time) / (next_time - curr_time), 0.0, 1.0)
        return curr_snap, next_snap, t

    def _draw(self) -> None:
        assert self.screen is not None
        self._draw_background()
        curr, nxt, t = self._get_transition()
        self._draw_table(curr, nxt, t)
        self._draw_side_panel(curr)
        self._draw_bottom_panel(curr)
        self._draw_header(curr)
        self._draw_time_corner(curr)
        if self.show_help:
            self._draw_help()

    def _draw_background(self) -> None:
        assert self.screen is not None
        self.screen.fill(self.BG)
        for y in range(self.HEIGHT):
            blend = y / self.HEIGHT
            color = (
                int(lerp(self.BG[0], self.BG2[0], blend)),
                int(lerp(self.BG[1], self.BG2[1], blend)),
                int(lerp(self.BG[2], self.BG2[2], blend)),
            )
            pygame.draw.line(self.screen, color, (0, y), (self.WIDTH, y))

    def _draw_header(self, snapshot: Snapshot) -> None:
        assert self.screen is not None
        title = self.big.render("Codexion Visualizer", True, self.TEXT)
        self.screen.blit(title, (24, 16))
        mode = "STEP" if self.step_mode else ("PAUSED" if self.paused else "PLAYING")
        mode2 = "EVENT" if self.event_mode else "TIME"
        line1 = f"t={snapshot.current_time_ms} ms   event={snapshot.next_event_index}/{len(self.sim.events)}   mode={mode}   run={mode2}"
        line2 = f"1ms -> {self.ms_to_seconds:.3f}s    event gap={self.event_gap_seconds:.2f}s"
        self.screen.blit(self.small.render(line1, True, self.MUTED), (24, 56))
        self.screen.blit(self.small.render(line2, True, self.MUTED), (24, 78))

        cfg = self.sim.config
        cfg_text = (
            f"coders={cfg.number_of_coders}  burnout={cfg.time_to_burnout}  compile={cfg.time_to_compile}  debug={cfg.time_to_debug}  "
            f"refactor={cfg.time_to_refactor}  required={cfg.number_of_compiles_required}  cooldown={cfg.dongle_cooldown}  scheduler={cfg.scheduler}"
        )
        self.screen.blit(self.tiny.render(cfg_text, True, self.MUTED), (24, 102))

    def _draw_time_corner(self, snapshot: Snapshot) -> None:
        assert self.screen is not None
        rect = pygame.Rect(self.WIDTH - self.SIDE_W - 180, 20, 150, 56)
        pygame.draw.rect(self.screen, self.PANEL, rect, border_radius=16)
        pygame.draw.rect(self.screen, self.GRID, rect, 2, border_radius=16)
        self.screen.blit(
            self.tiny.render("CURRENT TIME", True, self.MUTED),
            (rect.x + 16, rect.y + 10),
        )
        self.screen.blit(
            self.font.render(f"{snapshot.current_time_ms} ms", True, self.TEXT),
            (rect.x + 16, rect.y + 26),
        )

    def _coder_position(self, coder_id: int) -> Tuple[int, int]:
        cx, cy = self.center
        n = self.sim.config.number_of_coders
        # Start from bottom center (π/2) and go counter-clockwise (subtract angle)
        angle = math.pi / 2 - ((coder_id - 1) * 2 * math.pi / max(n, 1))
        x = int(cx + math.cos(angle) * self.table_radius)
        y = int(cy + math.sin(angle) * self.table_radius)
        return x, y

    def _dongle_rest_position(self, dongle_id: int) -> Tuple[int, int]:
        # Dongle[i] is on left of coder[i], positioned between coder[i-1] and coder[i]
        # Shift left by using (dongle_id - 1.5) instead of (dongle_id - 0.5)
        # Start from bottom center (π/2) and go counter-clockwise
        angle = math.pi / 2 - (
            (dongle_id - 1.5) * 2 * math.pi / max(self.sim.config.number_of_coders, 1)
        )
        x = int(self.center[0] + math.cos(angle) * self.table_radius * 0.55)
        y = int(self.center[1] + math.sin(angle) * self.table_radius * 0.55)
        return x, y

    def _hand_anchor(self, coder_pos: Tuple[int, int], side: str) -> Tuple[int, int]:
        x, y = coder_pos
        if side == "left":
            return (x - 24, y + 2)
        return (x + 24, y + 2)

    def _draw_table(self, curr: Snapshot, nxt: Snapshot, t: float) -> None:
        assert self.screen is not None
        cx, cy = self.center

        shadow_rect = pygame.Rect(cx - 270, cy - 170, 540, 340)
        pygame.draw.ellipse(self.screen, (0, 0, 0, 50), shadow_rect)
        pygame.draw.circle(
            self.screen, self.TABLE_EDGE, (cx, cy), int(self.table_radius * 0.72)
        )
        pygame.draw.circle(
            self.screen, self.TABLE, (cx, cy), int(self.table_radius * 0.66)
        )

        comp = pygame.Rect(0, 0, 190, 120)
        comp.center = (cx, cy)
        pygame.draw.rect(self.screen, self.ACCENT, comp, border_radius=24)
        pygame.draw.rect(self.screen, (235, 243, 255), comp, 3, border_radius=24)
        self.screen.blit(
            self.font.render("COMPILER", True, (255, 255, 255)),
            (comp.x + 42, comp.y + 34),
        )
        self.screen.blit(
            self.tiny.render("quantum compiler", True, (240, 247, 255)),
            (comp.x + 30, comp.y + 68),
        )

        self._draw_dongles(curr, nxt, t)
        for coder_id in range(1, self.sim.config.number_of_coders + 1):
            self._draw_person(curr.coders[coder_id], coder_id)

    def _draw_person(self, coder: CoderState, coder_id: int) -> None:
        assert self.screen is not None
        x, y = self._coder_position(coder_id)
        color = self._state_color(coder.state)

        pygame.draw.ellipse(self.screen, (0, 0, 0), (x - 44, y + 54, 88, 20))
        pygame.draw.circle(self.screen, color, (x, y - 34), 18)
        pygame.draw.circle(self.screen, (247, 249, 251), (x, y - 34), 2)
        pygame.draw.line(self.screen, color, (x, y - 16), (x, y + 26), 8)
        pygame.draw.line(self.screen, color, (x, y - 2), (x - 24, y + 14), 7)
        pygame.draw.line(self.screen, color, (x, y - 2), (x + 24, y + 14), 7)
        pygame.draw.line(self.screen, color, (x, y + 26), (x - 18, y + 54), 7)
        pygame.draw.line(self.screen, color, (x, y + 26), (x + 18, y + 54), 7)

        badge = pygame.Rect(x - 24, y + 58, 48, 24)
        pygame.draw.rect(self.screen, self.PANEL, badge, border_radius=12)
        pygame.draw.rect(self.screen, self.GRID, badge, 2, border_radius=12)
        text = self.small.render(str(coder_id), True, self.TEXT)
        self.screen.blit(text, text.get_rect(center=badge.center))

        st = self._short_state(coder.state)
        self.screen.blit(self.tiny.render(st, True, self.TEXT), (x - 34, y + 86))

    def _draw_dongles(self, curr: Snapshot, nxt: Snapshot, t: float) -> None:
        assert self.screen is not None
        for did in range(1, self.sim.config.number_of_coders + 1):
            d = curr.dongles[did]
            x, y = self._dongle_rest_position(did)
            pos = (x, y)

            if d.status == "taken" and d.holder is not None:
                holder_pos = self._coder_position(d.holder)
                side = "left"
                coder = curr.coders[d.holder]
                if coder.right_dongle_id == did:
                    side = "right"
                anchor = self._hand_anchor(holder_pos, side)
                pos = anchor

            # smooth pickup animation from current snapshot to next one
            if did in curr.dongles and did in nxt.dongles:
                d2 = nxt.dongles[did]
                start = pos
                end = pos
                if (
                    d.status != "taken"
                    and d2.status == "taken"
                    and d2.holder is not None
                ):
                    holder_pos = self._coder_position(d2.holder)
                    coder2 = nxt.coders[d2.holder]
                    side2 = "right" if coder2.right_dongle_id == did else "left"
                    end = self._hand_anchor(holder_pos, side2)
                    start = self._dongle_rest_position(did)
                    pos = (
                        int(lerp(start[0], end[0], t)),
                        int(lerp(start[1], end[1], t)),
                    )
                elif d.status == "taken" and d2.status != "taken":
                    holder_pos = self._coder_position(d.holder or 1)
                    coder1 = curr.coders[d.holder] if d.holder in curr.coders else None
                    side1 = "left"
                    if coder1 and coder1.right_dongle_id == did:
                        side1 = "right"
                    start = self._hand_anchor(holder_pos, side1)
                    end = self._dongle_rest_position(did)
                    pos = (
                        int(lerp(start[0], end[0], t)),
                        int(lerp(start[1], end[1], t)),
                    )

            color = self._dongle_color(d)
            self._draw_dongle_icon(pos[0], pos[1], color, did, d)

    def _draw_dongle_icon(
        self, x: int, y: int, color: Tuple[int, int, int], did: int, dstate: DongleState
    ) -> None:
        assert self.screen is not None
        body = pygame.Rect(x - 18, y - 9, 30, 18)
        plug1 = pygame.Rect(x + 10, y - 5, 7, 4)
        plug2 = pygame.Rect(x + 10, y + 1, 7, 4)
        pygame.draw.rect(self.screen, color, body, border_radius=5)
        pygame.draw.rect(self.screen, (245, 248, 251), body, 2, border_radius=5)
        pygame.draw.rect(self.screen, (235, 240, 248), plug1, border_radius=2)
        pygame.draw.rect(self.screen, (235, 240, 248), plug2, border_radius=2)
        self.screen.blit(self.tiny.render(str(did), True, self.TEXT), (x - 4, y - 24))
        if dstate.status == "cooldown":
            self.screen.blit(
                self.tiny.render("cd", True, self.DONGLE_COOLDOWN), (x - 8, y + 12)
            )

    def _dongle_color(self, dongle: DongleState) -> Tuple[int, int, int]:
        if dongle.status == "taken":
            return self.DONGLE_TAKEN
        if dongle.status == "cooldown":
            return self.DONGLE_COOLDOWN
        return self.DONGLE_AVAIL

    def _state_color(self, state: str) -> Tuple[int, int, int]:
        return {
            STATE_IDLE: self.GRID,
            STATE_WAITING: self.WARN,
            STATE_COMPILING: self.ACCENT,
            STATE_DEBUGGING: self.GOOD,
            STATE_REFACTORING: self.ACCENT2,
            STATE_BURNED: self.DANGER,
        }.get(state, self.GRID)

    def _short_state(self, state: str) -> str:
        return {
            STATE_IDLE: "idle",
            STATE_WAITING: "waiting",
            STATE_COMPILING: "compile",
            STATE_DEBUGGING: "debug",
            STATE_REFACTORING: "refactor",
            STATE_BURNED: "burned",
        }.get(state, state)

    def _draw_side_panel(self, snapshot: Snapshot) -> None:
        assert self.screen is not None
        panel = pygame.Rect(self.WIDTH - self.SIDE_W, 0, self.SIDE_W, self.HEIGHT)
        pygame.draw.rect(self.screen, self.PANEL, panel)
        pygame.draw.line(
            self.screen, self.GRID, (panel.x, 0), (panel.x, self.HEIGHT), 2
        )

        y = 20
        self.screen.blit(self.big.render("Coders", True, self.TEXT), (panel.x + 18, y))
        y += 56
        for cid in range(1, self.sim.config.number_of_coders + 1):
            coder = snapshot.coders[cid]
            box = pygame.Rect(panel.x + 14, y, self.SIDE_W - 28, 98)
            pygame.draw.rect(self.screen, self.PANEL2, box, border_radius=18)
            pygame.draw.circle(
                self.screen,
                self._state_color(coder.state),
                (box.x + 28, box.y + 24),
                11,
            )
            self.screen.blit(
                self.font.render(f"Coder {cid}", True, self.TEXT),
                (box.x + 50, box.y + 10),
            )
            line1 = f"state={coder.state}   held={coder.dongles_held}   compiles={coder.compile_count}"
            line2 = f"left={coder.left_dongle_id}   right={coder.right_dongle_id}"
            line3 = f"last={coder.last_action or '-'}"
            self.screen.blit(
                self.tiny.render(line1, True, self.MUTED), (box.x + 16, box.y + 42)
            )
            self.screen.blit(
                self.tiny.render(line2, True, self.MUTED), (box.x + 16, box.y + 60)
            )
            self.screen.blit(
                self.tiny.render(line3, True, self.MUTED), (box.x + 16, box.y + 78)
            )
            y += 108

        y += 10
        self.screen.blit(
            self.small.render(
                "Dongle colors: green=available, blue=taken, orange=cooldown",
                True,
                self.MUTED,
            ),
            (panel.x + 18, y),
        )

    def _draw_bottom_panel(self, snapshot: Snapshot) -> None:
        assert self.screen is not None
        panel = pygame.Rect(
            0, self.HEIGHT - self.BOTTOM_H, self.WIDTH - self.SIDE_W, self.BOTTOM_H
        )
        pygame.draw.rect(self.screen, self.PANEL, panel)
        pygame.draw.line(self.screen, self.GRID, (0, panel.y), (panel.w, panel.y), 2)
        self.screen.blit(self.big.render("Logs", True, self.TEXT), (20, panel.y + 14))

        prog_x = 20
        prog_y = panel.y + 56
        prog_w = panel.w - 40
        pygame.draw.rect(
            self.screen, self.GRID, (prog_x, prog_y, prog_w, 14), border_radius=8
        )
        fill = (
            0
            if self.max_time == 0
            else int(prog_w * (snapshot.current_time_ms / self.max_time))
        )
        pygame.draw.rect(
            self.screen, self.ACCENT, (prog_x, prog_y, fill, 14), border_radius=8
        )

        visible_lines = 7
        events = snapshot.recent_events
        if not events:
            return
        max_scroll = max(0, len(events) - visible_lines)
        self.logs_scroll = int(clamp(self.logs_scroll, 0, max_scroll))
        start = max(0, len(events) - visible_lines - self.logs_scroll)
        view = events[start : start + visible_lines]

        y = panel.y + 86
        for ev in view:
            text = ev.raw if self.show_timestamps else re.sub(r"^\d+\s+", "", ev.raw)
            self.screen.blit(self.small.render(text, True, self.TEXT), (24, y))
            y += 26

        info = f"scroll={self.logs_scroll}/{max_scroll}   W/S or wheel"
        self.screen.blit(
            self.tiny.render(info, True, self.MUTED), (24, panel.bottom - 26)
        )

    def _draw_help(self) -> None:
        assert self.screen is not None
        txt = "Enter step/auto   E event mode   Space next/pause   ←/→ step   ↑/↓ speed   Shift+↑/↓ event gap   W/S scroll logs   R restart   G end   T timestamps   H help   Q quit"
        rect = pygame.Rect(18, 126, self.WIDTH - self.SIDE_W - 36, 44)
        pygame.draw.rect(self.screen, self.PANEL, rect, border_radius=14)
        pygame.draw.rect(self.screen, self.GRID, rect, 2, border_radius=14)
        self.screen.blit(
            self.tiny.render(txt, True, self.MUTED), (rect.x + 12, rect.y + 14)
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Visualize codexion logs")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Path to a log file")
    source.add_argument(
        "--stdin", action="store_true", help="Read log lines from stdin"
    )
    source.add_argument("--run", nargs=argparse.REMAINDER, help="Run codexion directly")

    parser.add_argument("--coders", type=int, required=False)
    parser.add_argument("--burnout", type=int)
    parser.add_argument("--compile", dest="compile_ms", type=int)
    parser.add_argument("--debug", dest="debug_ms", type=int)
    parser.add_argument("--refactor", dest="refactor_ms", type=int)
    parser.add_argument("--required", type=int)
    parser.add_argument("--cooldown", type=int)
    parser.add_argument("--scheduler", choices=["fifo", "edf"])
    parser.add_argument(
        "--scale",
        type=float,
        default=0.02,
        help="How many real seconds 1 simulated ms should take",
    )
    parser.add_argument("--step", action="store_true", help="Start in step mode")
    return parser


def read_lines_from_source(
    args: argparse.Namespace,
) -> Tuple[List[str], Optional[List[str]]]:
    run_cmd = None
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read().splitlines(), run_cmd
    if args.stdin:
        return sys.stdin.read().splitlines(), run_cmd
    if args.run:
        run_cmd = args.run
        proc = subprocess.run(run_cmd, capture_output=True, text=True, check=False)
        lines = (proc.stdout or "").splitlines()
        if not lines and proc.stderr:
            print(proc.stderr, file=sys.stderr)
        return lines, run_cmd
    raise ValueError("No input source provided")


def infer_config(
    args: argparse.Namespace, run_cmd: Optional[List[str]], events: List[Event]
) -> Config:
    cmd_numbers: List[str] = []
    scheduler: Optional[str] = args.scheduler
    if run_cmd and len(run_cmd) >= 9:
        cmd_numbers = run_cmd[1:]
        if scheduler is None and len(cmd_numbers) >= 8:
            scheduler = cmd_numbers[7]

    number_of_coders = args.coders
    if number_of_coders is None and cmd_numbers:
        try:
            number_of_coders = int(cmd_numbers[0])
        except ValueError:
            pass
    if number_of_coders is None and events:
        number_of_coders = max(e.coder_id for e in events)
    if number_of_coders is None:
        raise SystemExit("Could not infer number of coders. Pass --coders N")

    def from_cmd(idx: int) -> Optional[int]:
        try:
            return int(cmd_numbers[idx])
        except (IndexError, ValueError):
            return None

    return Config(
        number_of_coders=number_of_coders,
        time_to_burnout=args.burnout if args.burnout is not None else from_cmd(1),
        time_to_compile=args.compile_ms if args.compile_ms is not None else from_cmd(2),
        time_to_debug=args.debug_ms if args.debug_ms is not None else from_cmd(3),
        time_to_refactor=(
            args.refactor_ms if args.refactor_ms is not None else from_cmd(4)
        ),
        number_of_compiles_required=(
            args.required if args.required is not None else from_cmd(5)
        ),
        dongle_cooldown=args.cooldown if args.cooldown is not None else from_cmd(6),
        scheduler=scheduler,
    )


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    lines, run_cmd = read_lines_from_source(args)
    events = LogParser.parse_lines(lines)
    if not events:
        raise SystemExit("No valid log lines found")
    config = infer_config(args, run_cmd, events)
    simulator = Simulator(config, events)
    visualizer = Visualizer(simulator, ms_to_seconds=args.scale, step_mode=args.step)
    visualizer.run()


if __name__ == "__main__":
    main()
