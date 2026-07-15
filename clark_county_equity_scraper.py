import base64, csv, io, json, os, re, sys, time
from datetime import datetime
from playwright.sync_api import sync_playwright

BASE        = "https://clarkcountyauditor.org"
CLASSES     = ["520", "530"]
CUTOFF      = datetime(2019, 12, 31)
CAP         = 500
BATCH       = 8
PAUSE       = 0.4
HEADLESS    = True
OUTPUT_FILE = "clark_county_equity_results.csv"
PARCELS_CACHE, PARTS_CACHE, SALES_CACHE = "parcels.json", "done_parts.json", "sales_cache.json"
TODAY = datetime.now()

def money(s):
    s = re.sub(r"[^\d.]", "", str(s or ""))
    try: return int(float(s))
    except (ValueError, TypeError): return 0

def pdate(s):
    s = str(s or "").strip()
    for f in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try: return datetime.strptime(s, f)
        except ValueError: continue
    return None

def equity_pct(appraised, price):
    if appraised <= 0 or price <= 0: return None
    return round((appraised - price) / appraised * 100, 1)

def load(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception: return default
    return default

def save(path, obj):
    with open(path, "w") as f: json.dump(obj, f)

def decode_csv(raw):
    if   raw.startswith(b"\xff\xfe"): text = raw.decode("utf-16-le", "replace").lstrip("\ufeff")
    elif raw.startswith(b"\xfe\xff"): text = raw.decode("utf-16-be", "replace").lstrip("\ufeff")
    elif raw.startswith(b"\xef\xbb\xbf"): text = raw.decode("utf-8-sig", "replace")
    else:
        try: text = raw.decode("utf-8")
        except UnicodeDecodeError: text = raw.decode("utf-16", "replace").lstrip("\ufeff")
    text  = text.replace("\r\n", "\n").replace("\r", "\n")
    lines, delim = text.split("\n"), ","
    if lines and lines[0].lower().startswith("sep="):
        d = lines[0][4:].rstrip("\n\r")
        if d: delim = d[0]
        lines = lines[1:]
    return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delim))

class BlockedError(RuntimeError): pass

class Client:
    def __init__(self, page):
        self.page = page
        self.districts, self.neighborhoods = [], []

    def open(self, attempts=4):
        last = None
        for i in range(attempts):
            try:
                self.page.goto(f"{BASE}/Search", wait_until="domcontentloaded", timeout=60000)
                if "Just a moment" in self.page.title():
                    raise BlockedError("Cloudflare challenge on /Search")
                self.page.wait_for_selector("#advancedSearchForm", state="attached", timeout=30000)
                self.page.wait_for_timeout(600)
                break
            except BlockedError: raise
            except Exception as e:
                last = e
                print(f"  open attempt {i+1} failed; retrying...")
                self.page.wait_for_timeout(3000)
        else:
            raise RuntimeError(f"Could not load search page: {last}")
        self.districts = json.loads(self.page.evaluate(
            "JSON.stringify(Array.from(document.querySelectorAll('[name=\"searchValues.LocationTaxDistrict\"] option')).map(o=>o.value).filter(v=>v))"))
        self.neighborhoods = json.loads(self.page.evaluate(
            "JSON.stringify(Array.from(document.querySelectorAll('[name=\"searchValues.LocationNeighborhood\"] option')).map(o=>o.value).filter(v=>v))"))
        print(f"  session ok | {len(self.districts)} districts | {len(self.neighborhoods)} neighborhoods")

    def _params(self, cls, dist, nbhd):
        p = [["SearchValues.LandPropertyClass[0]", cls]]
        if dist: p.append(["SearchValues.LocationTaxDistrict[0]", dist])
        if nbhd: p.append(["SearchValues.LocationNeighborhood[0]", nbhd])
        return p

    def _post(self, path, params, binary=False):
        js = """async ([path, params, wantBinary]) => {
  const form = document.getElementById('advancedSearchForm');
  const tok  = form.querySelector('[name=__RequestVerificationToken]').value;
  const body = new URLSearchParams();
  for (const kv of params) body.append(kv[0], kv[1]);
  body.append('Command','Advanced'); body.append('InvertSort','False');
  body.append('__RequestVerificationToken', tok);
  const r = await fetch(path, {method:'POST', body, credentials:'include',
      headers:{'Content-Type':'application/x-www-form-urlencoded'}});
  if (!wantBinary) {
    const t = await r.text();
    const m = t.match(/([0-9,]+)\\s+Parcels?/);
    return JSON.stringify({status:r.status, count:m?m[1]:null, challenged:t.indexOf('Just a moment')>=0});
  }
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin=''; const CH=8192;
  for (let i=0;i<bytes.length;i+=CH) bin+=String.fromCharCode.apply(null,bytes.subarray(i,i+CH));
  return JSON.stringify({status:r.status, b64:btoa(bin)});
}"""
        return json.loads(self.page.evaluate(js, [path, params, binary]))

    def count(self, cls, dist=None, nbhd=None):
        r = self._post(f"{BASE}/SearchResults", self._params(cls, dist, nbhd))
        if r.get("challenged") or r["status"] == 403: raise BlockedError("Cloudflare on search")
        if r["status"] != 200: raise RuntimeError(f"search HTTP {r['status']}")
        if r["count"] is None: raise RuntimeError("no parcel count in page")
        return int(r["count"].replace(",", ""))

    def export(self, cls, dist=None, nbhd=None):
        r = self._post(f"{BASE}/SearchResults/ExportToCSV", self._params(cls, dist, nbhd), binary=True)
        if r["status"] == 403: raise BlockedError("Cloudflare on export")
        if r["status"] != 200: raise RuntimeError(f"export HTTP {r['status']}")
        rows = decode_csv(base64.b64decode(r["b64"]))
        return [x for x in rows if (x.get("Parcel") or "").strip()]

    def sales_batch(self, parcels):
        js = """async ([parcels, base]) => {
  const one = async (pn) => {
    try {
      const r = await fetch(base+'/Parcel?Parcel='+pn, {credentials:'include'});
      if (!r.ok) return [pn, null, r.status];
      const html = await r.text();
      if (html.indexOf('Just a moment')>=0) return [pn, null, 403];
      const doc = new DOMParser().parseFromString(html,'text/html');
      for (const t of doc.querySelectorAll('table')) {
        const hdrs = Array.from(t.querySelectorAll('th')).map(x=>x.textContent.trim());
        const iD=hdrs.indexOf('Sales Date'), iA=hdrs.indexOf('Amount');
        if (iD<0||iA<0) continue;
        const iT=hdrs.indexOf('Deed Type'), iV=hdrs.indexOf('Valid');
        const rows=[];
        for (const tr of Array.from(t.querySelectorAll('tr')).slice(1)) {
          const c=Array.from(tr.querySelectorAll('td')).map(x=>x.textContent.trim());
          if (c.length<=Math.max(iD,iA)) continue;
          rows.push({date:c[iD],amt:c[iA],deed:(iT>=0&&c.length>iT)?c[iT]:'',valid:(iV>=0&&c.length>iV)?c[iV]:''});
        }
        return [pn, rows, 200];
      }
      return [pn, [], 200];
    } catch(e) { return [pn, null, -1]; }
  };
  const out=[];
  for (const p of parcels) out.push(await one(p));
  return JSON.stringify(out);
}"""
        out = json.loads(self.page.evaluate(js, [parcels, BASE]))
        if any(st == 403 for _, _, st in out): raise BlockedError("Cloudflare on parcel page")
        return {pn: rows for pn, rows, st in out if rows is not None}


def stage1(c):
    rows = load(PARCELS_CACHE, [])
    done = set(load(PARTS_CACHE, []))
    if rows: print(f"  resuming: {len(rows)} rows, {len(done)} partitions cached")

    def add(key, got, cls):
        for g in got: g["_class"] = cls
        rows.extend(got); done.add(key)
        save(PARCELS_CACHE, rows); save(PARTS_CACHE, list(done))

    for cls in CLASSES:
        total = c.count(cls)
        print(f"\n-- Class {cls}: {total}{'  [AT CAP]' if total >= CAP else ''}")
        if total < CAP:
            k = f"{cls}|ALL"
            if k not in done:
                got = c.export(cls); add(k, got, cls)
                print(f"   exported {len(got)}")
            continue
        for d in c.districts:
            k = f"{cls}|D{d}"
            if k in done: continue
            n = c.count(cls, dist=d)
            if n == 0:
                done.add(k); save(PARTS_CACHE, list(done)); continue
            if n < CAP:
                got = c.export(cls, dist=d); add(k, got, cls)
                print(f"   district {d}: {n} -> {len(got)}")
            else:
                print(f"   district {d}: {n} [AT CAP] -> splitting by neighborhood")
                subs = [x for x in c.neighborhoods if x.startswith(d)]
                rec = 0
                for nb in subs:
                    kn = f"{cls}|N{nb}"
                    if kn in done: continue
                    got = c.export(cls, nbhd=nb)
                    add(kn, got, cls); rec += len(got)
                done.add(k); save(PARTS_CACHE, list(done))
                print(f"      -> {len(subs)} neighborhoods, {rec} rows")

    uniq = {}
    for r in rows:
        pn = (r.get("Parcel") or "").strip()
        if pn: uniq[pn] = r
    uniq = list(uniq.values())
    print(f"\nStage 1: {len(rows)} raw rows -> {len(uniq)} unique parcels")
    return uniq


def stage2(c, uniq):
    cache = load(SALES_CACHE, {})
    todo = [r["Parcel"] for r in uniq if money(r.get("Last Sale Price")) == 0 and r["Parcel"] not in cache]
    print(f"\nStage 2: {len(todo)} parcels with $0 sale ({len(cache)} cached)")
    if not todo: return cache
    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i+BATCH]
        try:
            got = c.sales_batch(chunk)
        except BlockedError:
            save(SALES_CACHE, cache)
            print("\n  *** Cloudflare block. Progress saved. Wait 20-30 min and re-run. ***")
            raise
        cache.update(got)
        if (i // BATCH) % 10 == 0:
            save(SALES_CACHE, cache)
            print(f"   {min(i+BATCH,len(todo))}/{len(todo)} | {time.time()-t0:.0f}s", flush=True)
        time.sleep(PAUSE)
    save(SALES_CACHE, cache)
    return cache


def stage3(uniq, cache):
    results, no_real, after_cut = [], 0, 0
    for r in uniq:
        pn = (r.get("Parcel") or "").strip()
        appraised = money(r.get("Appraised Value"))
        price = money(r.get("Last Sale Price"))
        date = pdate(r.get("Last Sale Date"))
        deed, valid, note = "", r.get("Is Valid", ""), ""
        if price == 0:
            hist = cache.get(pn)
            if hist is None: no_real += 1; continue
            real = next((h for h in sorted(hist, key=lambda x: (pdate(x["date"]) or datetime.min), reverse=True) if money(h["amt"]) > 0), None)
            if not real: no_real += 1; continue
            date, price = pdate(real["date"]), money(real["amt"])
            deed, valid, note = real["deed"], real["valid"], "resolved from $0 transfer"
        if not date or date > CUTOFF: after_cut += 1; continue
        results.append({
            "Parcel": pn, "Class": r.get("_class",""),
            "Owner (Last Buyer)": r.get("Last Buyer",""),
            "Address": r.get("Address",""), "City": r.get("City",""),
            "Appraised ($)": appraised, "Last Real Sale ($)": price,
            "Last Real Sale": date.strftime("%m/%d/%Y"),
            "Years Held": round((TODAY - date).days / 365.25, 1),
            "Equity %": equity_pct(appraised, price),
            "Est. Equity ($)": (appraised - price) if (appraised and price) else "",
            "Taxes Due": r.get("Net Due",""), "Deed Type": deed, "Valid Sale": valid,
            "Neighborhood": r.get("Neighborhood Code",""), "Acres": r.get("Acres",""),
            "Note": note, "Parcel URL": f"{BASE}/Parcel?Parcel={pn}",
        })
    results.sort(key=lambda x: (x["Equity %"] if x["Equity %"] is not None else -999), reverse=True)
    print(f"\n{'='*60}\n  unique: {len(uniq)}  no real sale: {no_real}  after cutoff: {after_cut}  QUALIFYING: {len(results)}\n{'='*60}\n")
    if not results: print("No qualifying properties."); return
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader(); w.writerows(results)
    print(f"Saved {len(results)} -> {OUTPUT_FILE}")
    print(f"{'Address':<44}{'Apprsd':>10}{'Paid':>10}{'Eq%':>7}{'Sold':>12}")
    for r in results[:15]:
        print(f"{r['Address'][:43]:<44}{r['Appraised ($)']:>10,}{r['Last Real Sale ($)']:>10,}{str(r['Equity %']):>7}{r['Last Real Sale']:>12}")


def main():
    t0 = time.time()
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=HEADLESS)
        c = Client(b.new_context().new_page())
        print("Opening session...")
        try:
            c.open()
            uniq  = stage1(c)
            cache = stage2(c, uniq)
        except BlockedError as e:
            print(f"\nSTOPPED: {e}\nProgress saved. Re-run later to resume.")
            b.close(); sys.exit(1)
        b.close()
    stage3(uniq, cache)
    print(f"\nTotal: {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
