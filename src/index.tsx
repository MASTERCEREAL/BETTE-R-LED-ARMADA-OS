import {
  ButtonItem,
  ConfirmModal,
  showModal,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaLightbulb } from "react-icons/fa";

interface Profile {
  name: string;
  hue: number;
  brightness: number;
  color: [number, number, number];
  react?: boolean;
  ambi?: boolean;
}

interface Settings {
  enabled: boolean;
  profiles_enabled: boolean;
  profiles: Record<string, Profile>;
  app_id: string;
  show_debug?: boolean;
  rain?: boolean;
  home_on?: boolean;
  home_hue?: number;
}

interface Game {
  id: string;
  name: string;
}

const getSettings = callable<[], Settings>("get_settings");
const setEnabled = callable<[enabled: boolean], boolean>("set_enabled");
const setProfilesEnabled = callable<[enabled: boolean], boolean>("set_profiles_enabled");
const setRunningGame = callable<[appId: string], void>("set_running_game");
const gameClosing = callable<[appId: string], void>("game_closing");
const saveProfile = callable<
  [appId: string, name: string, hue: number, brightness: number, color: number[], react: boolean, ambi: boolean],
  Record<string, Profile>
>("save_profile");
const deleteProfile = callable<[appId: string], Record<string, Profile>>("delete_profile");
const setHome = callable<[on: boolean, hue: number], { home_on: boolean; home_hue: number }>("set_home");
const setRain = callable<[enabled: boolean], boolean>("set_rain");
const setShowDebug = callable<[show: boolean], boolean>("set_show_debug");
const deleteAllProfiles = callable<[], Record<string, Profile>>("delete_all_profiles");
interface Status {
  sticks: boolean;
  leds: string[];
  service: string;
  battery: string;
  checkin: number;
  asleep: boolean;
  loop: string;
  showing: string;
  error: string;
  write_check: string;
  react?: string;
  ambi?: string;
  method: string;
  commands?: { service?: string[]; armada_control?: string[]; tool?: string };
  write_error: string;
  others: string[];
  paused: string[];
  all_leds: string[];
  stick_count: number;
}
const getStatus = callable<[], Status>("get_status");
const setPreview = callable<[color: number[], level: number, react: boolean, ambi: boolean, flash: boolean], void>(
  "set_preview",
);
const endPreview = callable<[], void>("end_preview");

const DEFAULT_BRIGHTNESS = 5; // % for a game with no saved color yet

function hsvToRgb(h: number, v: number): [number, number, number] {
  const c = v;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const [r, g, b] =
    h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}

function runningGame(): Game | null {
  const app = Router.MainRunningApp;
  return app ? { id: String(app.appid), name: app.display_name } : null;
}

function Content() {
  const [settings, setSettings] = useState<Settings | null>(null);
  // The game being played, plus its color and brightness, in one state so they
  // always change together (separate updates let a render slip through with the
  // defaults, which flashed the default color on the sticks).
  const [editor, setEditor] = useState<{
    game: Game | null;
    hue: number;
    brightness: number;
    react: boolean;
    ambi: boolean;
    unset: boolean; // no color picked yet for this game: the sticks blink white
  }>({
    game: null,
    hue: 30,
    brightness: DEFAULT_BRIGHTNESS,
    react: false,
    ambi: false,
    unset: false,
  });
  const { game, hue, brightness, react, ambi, unset } = editor;
  const saveTimer = useRef<number>();
  const [status, setStatus] = useState<Status | null>(null);
  const profilesRef = useRef<Record<string, Profile>>({});
  profilesRef.current = settings?.profiles ?? {};
  const settingsRef = useRef<Settings | null>(null);
  settingsRef.current = settings;

  // Load the running game's saved color (or the defaults) into the sliders.
  const load = (g: Game | null, profiles: Record<string, Profile>) =>
    setEditor({
      game: g,
      hue: (g && profiles[g.id]?.hue) ?? 30,
      brightness: (g && profiles[g.id]?.brightness) ?? DEFAULT_BRIGHTNESS,
      react: !!(g && profiles[g.id]?.react),
      ambi: !!(g && profiles[g.id]?.ambi),
      unset: !!g && !(g.id in profiles),
    });

  useEffect(() => {
    let current = "";
    getSettings().then((s) => {
      const g = runningGame();
      current = g?.id ?? "";
      load(g, s.profiles);
      setSettings(s);
    });
    const refreshStatus = () => {
      // Only fetch status while debug info is shown (or the backend isn't answering yet).
      const s = settingsRef.current;
      if (s && !s.show_debug) return;
      getStatus().then(setStatus).catch((e) => setStatus({ error: String(e) } as Status));
    };
    refreshStatus();
    const statusId = window.setInterval(refreshStatus, 1000);
    // Follow the game if one starts or quits while the menu is open.
    const id = window.setInterval(() => {
      const g = runningGame();
      if ((g?.id ?? "") !== current) {
        current = g?.id ?? "";
        load(g, profilesRef.current);
      }
    }, 2000);
    return () => {
      window.clearInterval(id);
      window.clearInterval(statusId);
      window.clearTimeout(saveTimer.current);
      endPreview();
    };
  }, []);

  // Full-strength color plus a separate brightness: the backend applies the LED
  // color correction to the color only, so a low brightness stays visible.
  const color = hsvToRgb(hue, 1);
  const level = brightness / 100;
  const editing = !!settings?.profiles_enabled && !!game;

  // Show the color being edited on the LEDs, refreshed while the menu stays open.
  useEffect(() => {
    if (!editing) {
      endPreview();
      return;
    }
    setPreview(color, level, react, ambi, unset);
    const id = window.setInterval(() => setPreview(color, level, react, ambi, unset), 4000);
    return () => window.clearInterval(id);
  }, [editing, color.join(","), level, react, ambi, unset]);

  const statusView = status && (

        <PanelSection title="Status">
          <div style={{ fontSize: 11, opacity: 0.75, lineHeight: 1.5, wordBreak: "break-word" }}>
            <div>Stick LEDs: {status.sticks ? "found" : `not found${status.leds?.length ? ` (other: ${status.leds.join(", ")})` : ""}`}</div>
            <div>Armada LED service: {status.service}</div>
            <div>Battery: {status.battery}{status.asleep ? " · asleep" : ""}</div>
            <div>Game mode check-in: {status.checkin < 0 ? "never" : `${status.checkin}s ago`}</div>
            <div>
              Loop: {status.loop} · showing: {status.showing}
            </div>
            {status.error && <div style={{ color: "#ff8080" }}>Error: {status.error}</div>}
            <div>Paused while BETT-R LED has the lights: {status.paused?.length ? status.paused.join(" | ") : "nothing"}</div>
            <div>Writing via: {status.method}</div>
            {status.commands && (
              <>
                <div>Armada service commands: {(status.commands.service ?? []).join(", ") || "none"}</div>
                <div>Armada Control uses: {(status.commands.armada_control ?? []).join(", ") || "none"}</div>
                <div>Armada LED tool: {status.commands.tool}</div>
              </>
            )}
            <div>LED React: {status.react ?? "off"}</div>
            <div>Ambilight: {status.ambi ?? "off"}</div>
            <div>Write check: {status.write_check}</div>
            {status.write_error && <div style={{ color: "#ff8080" }}>Write error: {status.write_error}</div>}
            <div>Stick LED files: {status.stick_count} of 24</div>
            <div>Other LEDs: {status.all_leds?.length ? status.all_leds.join(", ") : "none"}</div>
            <div>Other LED programs: {status.others?.length ? status.others.join(" | ") : "none"}</div>
          </div>
        </PanelSection>
  );

  // Backend not answering: still show the status so the problem is visible.
  if (!settings) return <>{statusView}</>;

  const hasProfile = !!game && game.id in settings.profiles;

  // Slider changes save automatically (debounced) for the running game.
  const update = (h: number, b: number, r: boolean = react, a: boolean = ambi) => {
    setEditor({ game, hue: h, brightness: b, react: r, ambi: a, unset: false });
    if (!game) return;
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(async () => {
      const profiles = await saveProfile(game.id, game.name, h, b, hsvToRgb(h, 1), r, a);
      setSettings((s) => s && { ...s, profiles });
    }, 400);
  };

  // Home screen light: switch and color apply straight away.
  const homeOn = settings.home_on ?? true;
  const homeHue = settings.home_hue ?? 30;
  const updateHome = async (on: boolean, h: number) => {
    setSettings((s) => s && { ...s, home_on: on, home_hue: h });
    const r = await setHome(on, h);
    setSettings((s) => s && { ...s, ...r });
  };

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ToggleField
            label="LEDs"
            description="Home screen, and games without their own color"
            checked={homeOn}
            onChange={(v) => updateHome(v, homeHue)}
          />
        </PanelSectionRow>
        {homeOn && !settings.rain && (
          <>
            <PanelSectionRow>
              <div style={{ height: 24, borderRadius: 4, background: `rgb(${hsvToRgb(homeHue, 1).join(",")})` }} />
            </PanelSectionRow>
            <PanelSectionRow>
              <SliderField
                label="Home color"
                value={homeHue}
                min={0}
                max={355}
                step={5}
                onChange={(h) => updateHome(homeOn, h)}
              />
            </PanelSectionRow>
          </>
        )}
        <PanelSectionRow>
          <ToggleField
            label="Breathe while charging"
            description="Breathes orange while plugged in (red when below 10%)"
            checked={settings.enabled}
            onChange={async (v) => {
              const enabled = await setEnabled(v);
              setSettings((s) => s && { ...s, enabled });
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="LED Rain Mode"
            description="White raindrops on the sticks on the home screen (no game running)"
            checked={!!settings.rain}
            onChange={async (v) => {
              const rain = await setRain(v);
              setSettings((s) => s && { ...s, rain });
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Per-game LED colors"
            checked={settings.profiles_enabled}
            onChange={async (v) => {
              const profiles_enabled = await setProfilesEnabled(v);
              setSettings((s) => s && { ...s, profiles_enabled });
            }}
          />
        </PanelSectionRow>
      </PanelSection>

      {settings.profiles_enabled && (
        <PanelSection title="Game profile">
          {!game ? (
            <PanelSectionRow>
              <div style={{ fontSize: 12, opacity: 0.7, padding: "4px 0" }}>
                Open a game to set its LED color.
              </div>
            </PanelSectionRow>
          ) : (
            <>
              <PanelSectionRow>
                <div style={{ padding: "4px 0" }}>
                  <div style={{ fontWeight: "bold" }}>{game.name}</div>
                  <div style={{ fontSize: 12, opacity: 0.7 }}>
                    {hasProfile ? "Color saved for this game" : "Move a slider to save a color for this game"}
                  </div>
                </div>
              </PanelSectionRow>
              {!ambi && (
                <>
                  <PanelSectionRow>
                    <div style={{ height: 24, borderRadius: 4, background: unset ? "#fff" : `rgb(${hsvToRgb(hue, 1).join(",")})` }} />
                  </PanelSectionRow>
                  <PanelSectionRow>
                    <SliderField label="Color" value={hue} min={0} max={355} step={5} onChange={(h) => update(h, brightness)} />
                  </PanelSectionRow>
                </>
              )}
              {!react && (
                <PanelSectionRow>
                  <SliderField
                    label="Brightness"
                    value={brightness}
                    min={1}
                    max={100}
                    step={1}
                    onChange={(b) => update(hue, b)}
                  />
                </PanelSectionRow>
              )}
              <PanelSectionRow>
                <ToggleField
                  label="LED React"
                  description="Pulses with the game's sound and flashes on hits (up to 12%); each stick follows its own side"
                  checked={react}
                  onChange={(r) => update(hue, brightness, r)}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <ToggleField
                  label="Ambilight"
                  description="Uses the main color on screen instead of your color; each stick follows its side of the screen"
                  checked={ambi}
                  onChange={(a) => update(hue, brightness, react, a)}
                />
              </PanelSectionRow>
              {hasProfile && (
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={async () => {
                      window.clearTimeout(saveTimer.current);
                      const profiles = await deleteProfile(game.id);
                      setSettings((s) => s && { ...s, profiles });
                      load(game, profiles);
                    }}
                  >
                    Remove color for this game
                  </ButtonItem>
                </PanelSectionRow>
              )}
            </>
          )}
          {Object.keys(settings.profiles).length > 0 && (
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() =>
                  showModal(
                    <ConfirmModal
                      strTitle="Delete all profiles?"
                      strDescription={`This removes the saved LED color for all ${Object.keys(settings.profiles).length} games. It can't be undone.`}
                      strOKButtonText="Delete all"
                      onOK={async () => {
                        window.clearTimeout(saveTimer.current);
                        const profiles = await deleteAllProfiles();
                        setSettings((s) => s && { ...s, profiles });
                        load(game, profiles);
                      }}
                    />,
                  )
                }
              >
                Delete all profiles
              </ButtonItem>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      <PanelSection>
        <PanelSectionRow>
          <ToggleField
            label="Show debug info"
            checked={!!settings.show_debug}
            onChange={async (v) => {
              const show_debug = await setShowDebug(v);
              setSettings((s) => s && { ...s, show_debug });
              if (show_debug) getStatus().then(setStatus).catch(() => {});
            }}
          />
        </PanelSectionRow>
      </PanelSection>

      {settings.show_debug && statusView}

      <PanelSection>
        <div style={{ textAlign: "center", fontSize: 11, opacity: 0.6, letterSpacing: 1, padding: "8px 0" }}>
          MADE BY ILLMENTALITYAPPS
        </div>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // Tell the backend which game is running, even while the menu is closed. The regular
  // report doubles as a "game mode is open" check-in; when it stops (desktop mode,
  // Steam closed) the backend hands the LEDs back.
  const report = () => setRunningGame(runningGame()?.id ?? "");
  report();
  const lifetime = (window as any).SteamClient?.GameSessions?.RegisterForAppLifetimeNotifications?.(
    (n: { unAppID?: number; bRunning?: boolean }) => {
      // A game quitting: tell the backend at once so it stops screenshots/audio
      // before the game shuts down, then report the new state once Steam catches up.
      if (n && n.bRunning === false) gameClosing(String(n.unAppID ?? ""));
      setTimeout(report, 1000);
    },
  );
  const poll = setInterval(report, 5000);

  return {
    name: "BETT-R LED",
    titleView: <div className={staticClasses.Title}>BETT-R LED</div>,
    content: <Content />,
    icon: <FaLightbulb />,
    onDismount() {
      lifetime?.unregister?.();
      clearInterval(poll);
    },
  };
});
