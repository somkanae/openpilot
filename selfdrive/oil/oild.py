#!/usr/bin/env python3
"""Fuel log helper for HR-V e:HEV.

While onroad: samples odometer (0x516) and the per-ignition fuel counter (0x324 bytes 2-3)
from the latest CAN packet and keeps running totals in /data/oil/state.json.
Any time: when the UI creates /data/oil/request (Settings > Device > Fuel Log > UPLOAD),
queue a fill-up record and POST it to the Atlantis Oil tab. Failed posts stay queued and retry.
"""
import json, os, time, uuid, urllib.request

import cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

OIL_DIR = "/data/oil"
STATE = f"{OIL_DIR}/state.json"
CONFIG = f"{OIL_DIR}/config.json"      # {"url": ..., "token": ..., "units_per_litre": null}
QUEUE = f"{OIL_DIR}/queue.jsonl"
REQUEST = f"{OIL_DIR}/request"         # created by the UI button
STATUS = f"{OIL_DIR}/status.txt"       # shown under the UI button
LAST_PRICE = f"{OIL_DIR}/last_price.txt"  # default for the next price input
PT_BUS = 0
CTR_WRAP = 65536


def _num(txt):
  try:
    v = float(str(txt).replace(",", "").strip())
    return v if v > 0 else None
  except ValueError:
    return None


def parse_pump(raw: str) -> dict:
  """UI request: {"baht","price_per_l","litre"} as text; any two give the third. A bare number is baht."""
  try:
    d = json.loads(raw)
  except ValueError:
    d = None
  if not isinstance(d, dict):
    d = {"baht": raw}
  baht, price, litre = _num(d.get("baht", "")), _num(d.get("price_per_l", "")), _num(d.get("litre", ""))
  if baht and price and not litre:
    litre = baht / price
  elif baht and litre and not price:
    price = baht / litre
  elif price and litre and not baht:
    baht = price * litre
  r = lambda v, n: round(v, n) if v else None
  return {"baht": r(baht, 2), "price_per_l": r(price, 2), "litre": r(litre, 3)}


def load(path, default):
  try:
    with open(path) as f:
      return json.load(f)
  except Exception:
    return default


def save(path, obj):
  tmp = path + ".tmp"
  with open(tmp, "w") as f:
    json.dump(obj, f)
  os.replace(tmp, path)


def set_status(text):
  with open(STATUS, "w") as f:
    f.write(time.strftime("%d/%m %H:%M ") + text)


class FuelTracker:
  def __init__(self):
    # since_*: totals since the last upload. drive_ctr_*: current ignition cycle.
    self.s = load(STATE, {"odo": None, "coolant": None, "last_upload_odo": None,
                          "since_fuel_units": 0, "since_drive_s": 0.0,
                          "drive_ctr_last": None, "drive_wraps": 0})

  def drive_units(self):
    if self.s["drive_ctr_last"] is None:
      return 0
    return self.s["drive_ctr_last"] + CTR_WRAP * self.s["drive_wraps"]

  def new_drive(self, first_ctr, odo):
    # the counter resets every ignition cycle; a comma-only reboot keeps counting, so treat that as the same drive
    if self.s["drive_ctr_last"] is not None and first_ctr >= self.s["drive_ctr_last"] and odo == self.s["odo"]:
      return
    self.s["since_fuel_units"] += self.drive_units()
    self.s["drive_ctr_last"] = None
    self.s["drive_wraps"] = 0

  def update_ctr(self, v):
    last = self.s["drive_ctr_last"]
    if last is not None and v + 1000 < last:
      self.s["drive_wraps"] += 1
    self.s["drive_ctr_last"] = v

  def take_record(self, cfg):
    fuel_units = self.s["since_fuel_units"] + self.drive_units()
    odo, last = self.s["odo"], self.s["last_upload_odo"]
    trip_km = None if odo is None or last is None else odo - last
    drive_s = self.s["since_drive_s"]
    upl = cfg.get("units_per_litre")
    fuel_l = fuel_units / upl if upl else None
    rec = {
      "uid": str(uuid.uuid4()),
      "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
      "odo_km": odo,
      "trip_km": trip_km,
      "drive_s": round(drive_s),
      "avg_kph": round(trip_km / (drive_s / 3600), 1) if trip_km and drive_s > 60 else None,
      "fuel_units": fuel_units,
      "fuel_l_counter": round(fuel_l, 2) if fuel_l else None,
      "kmpl_counter": round(trip_km / fuel_l, 2) if fuel_l and trip_km else None,
      "coolant_c": self.s["coolant"],
      "device": "comma3-hrv",
    }
    # start the next trip from here
    self.s["last_upload_odo"] = odo
    self.s["since_fuel_units"] = -self.drive_units()  # keep counting the current drive from this point
    self.s["since_drive_s"] = 0.0
    return rec


def flush_queue(cfg) -> int:
  """Send the oldest queued record (Atlantis allows 6 req/min). Returns how many are still queued."""
  if not os.path.exists(QUEUE) or not cfg.get("url") or not cfg.get("token"):
    return 0
  with open(QUEUE) as f:
    pending = [json.loads(l) for l in f if l.strip()]
  left = pending[1:]
  for rec in pending[:1]:
    try:
      req = urllib.request.Request(cfg["url"], data=json.dumps(rec).encode(), method="POST",
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}"})
      with urllib.request.urlopen(req, timeout=10) as r:
        if r.status // 100 != 2:
          raise RuntimeError(f"HTTP {r.status}")
      set_status(f"sent: odo {rec['odo_km']} km, trip {rec['trip_km']} km")
    except Exception as e:
      left.insert(0, rec)
      set_status(f"queued ({len(pending)}), retrying: {type(e).__name__}")
      cloudlog.warning(f"oild: upload failed: {e}")
  with open(QUEUE + ".tmp", "w") as f:
    f.writelines(json.dumps(r) + "\n" for r in left)
  os.replace(QUEUE + ".tmp", QUEUE)
  return len(left)


def main():
  os.makedirs(OIL_DIR, exist_ok=True)
  params = Params()
  tracker = FuelTracker()
  can_sock = None
  onroad_prev = False
  first_ctr_seen = False
  odo_seen = False  # odometer read in this onroad session (state may hold a stale one)
  last_t = time.monotonic()
  last_save = last_retry = 0.0
  queued = 1  # try the leftover queue soon after start

  while True:
    now = time.monotonic()
    onroad = params.get_bool("IsOnroad")

    if onroad and not onroad_prev:
      can_sock = messaging.sub_sock("can", conflate=True)
      first_ctr_seen = False
      odo_seen = False
    if onroad_prev and not onroad:
      can_sock = None
      save(STATE, tracker.s)

    if onroad and can_sock is not None:
      tracker.s["since_drive_s"] += now - last_t
      msg = messaging.recv_one_or_none(can_sock)
      if msg is not None:
        for c in msg.can:
          if c.src != PT_BUS:
            continue
          if c.address == 0x516:
            tracker.s["odo"] = int.from_bytes(bytes(c.dat)[0:3], "big")
            odo_seen = True
          elif c.address == 0x324:
            d = bytes(c.dat)
            tracker.s["coolant"] = d[0] - 40
            v = int.from_bytes(d[2:4], "big")
            if not first_ctr_seen and odo_seen:
              tracker.new_drive(v, tracker.s["odo"])
              first_ctr_seen = True
            if first_ctr_seen:
              tracker.update_ctr(v)
    last_t = now
    onroad_prev = onroad

    if os.path.exists(REQUEST):
      with open(REQUEST) as f:
        pump = parse_pump(f.read())
      os.remove(REQUEST)
      cfg = load(CONFIG, {})
      rec = tracker.take_record(cfg)
      rec.update(pump)
      if pump["price_per_l"]:
        with open(LAST_PRICE, "w") as f:
          f.write(f"{pump['price_per_l']:g}")
      with open(QUEUE, "a") as f:
        f.write(json.dumps(rec) + "\n")
      save(STATE, tracker.s)
      set_status(f"queued: odo {rec['odo_km']} km, trip {rec['trip_km']} km")
      queued = flush_queue(cfg)
      last_retry = now

    if now - last_retry > (12 if queued else 60):
      queued = flush_queue(load(CONFIG, {}))
      last_retry = now
    if now - last_save > 10:
      save(STATE, tracker.s)
      last_save = now

    time.sleep(0.2)


if __name__ == "__main__":
  main()
