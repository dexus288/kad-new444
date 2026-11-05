"use strict";
/* Векторні тайли (MVT .pbf) для Leaflet без залежностей: декодер + шар на canvas.
   Малює межі ділянок, на великих масштабах — кадастрові номери. */
(() => {
  // ------------------------------------------------------------ декодер protobuf/MVT
  function reader(buf) {
    let pos = 0;
    const end = buf.length;
    const varint = () => {
      let r = 0, s = 0, b;
      do { b = buf[pos++]; r += (b & 0x7f) * 2 ** s; s += 7; } while (b >= 0x80);
      return r;
    };
    const fields = function* (from, to) {
      pos = from;
      while (pos < to) {
        const key = varint(), f = Math.floor(key / 8), w = key & 7;
        if (w === 0) yield [f, varint()];
        else if (w === 2) { const ln = varint(), s = pos; pos += ln; yield [f, [s, pos]]; }
        else if (w === 1) { const s = pos; pos += 8; yield [f, [s, pos]]; }
        else if (w === 5) { const s = pos; pos += 4; yield [f, [s, pos]]; }
        else throw new Error("MVT wire type " + w);
      }
    };
    const packed = ([s, e]) => { const save = pos, out = []; pos = s; while (pos < e) out.push(varint()); pos = save; return out; };
    return { fields, packed, end };
  }
  const td = new TextDecoder();
  const zz = (n) => (n % 2 ? -(n + 1) / 2 : n / 2);

  function decode(buf) {
    const r = reader(buf), layers = {};
    for (const [f, lr] of [...r.fields(0, r.end)]) {
      if (f !== 3) continue;
      let name = "", extent = 4096; const keys = [], values = [], feats = [];
      for (const [lf, lv] of [...r.fields(lr[0], lr[1])]) {
        if (lf === 1) name = td.decode(buf.subarray(lv[0], lv[1]));
        else if (lf === 2) feats.push(lv);
        else if (lf === 3) keys.push(td.decode(buf.subarray(lv[0], lv[1])));
        else if (lf === 4) {
          let val = null;
          for (const [vf, vv] of [...r.fields(lv[0], lv[1])]) {
            if (vf === 1) val = td.decode(buf.subarray(vv[0], vv[1]));
            else if (vf === 2) val = new DataView(buf.buffer, buf.byteOffset + vv[0], 4).getFloat32(0, true);
            else if (vf === 3) val = new DataView(buf.buffer, buf.byteOffset + vv[0], 8).getFloat64(0, true);
            else if (vf === 4 || vf === 5) val = vv;
            else if (vf === 6) val = zz(vv);
            else if (vf === 7) val = !!vv;
          }
          values.push(val);
        } else if (lf === 5) extent = lv;
      }
      layers[name] = {
        extent,
        features: feats.map((fr) => {
          let tags = [], type = 0, geom = [];
          for (const [ff, fv] of [...r.fields(fr[0], fr[1])]) {
            if (ff === 2) tags = r.packed(fv);
            else if (ff === 3) type = fv;
            else if (ff === 4) geom = r.packed(fv);
          }
          const props = {};
          for (let i = 0; i + 1 < tags.length; i += 2) props[keys[tags[i]]] = values[tags[i + 1]];
          return { type, props, rings: rings(geom) };
        }),
      };
    }
    return layers;
  }

  function rings(cmds) {
    let x = 0, y = 0, ring = null; const out = [];
    for (let i = 0; i < cmds.length;) {
      const c = cmds[i++], cmd = c & 7, cnt = c >> 3;
      if (cmd === 1 || cmd === 2) {
        for (let k = 0; k < cnt; k++) {
          x += zz(cmds[i++]); y += zz(cmds[i++]);
          if (cmd === 1) { ring = [[x, y]]; out.push(ring); } else if (ring) ring.push([x, y]);
        }
      } else if (cmd === 7 && ring) ring.push(ring[0]);
    }
    return out;
  }

  // ------------------------------------------------------------ шар Leaflet
  L.VectorParcels = L.GridLayer.extend({
    options: {
      url: "", layer: "parcels_ua", color: "#22d3ee", weight: 1.2, labelZoom: 17,
      labelColor: "#ffffff", labelHalo: "rgba(0,0,0,.75)",
    },

    createTile(coords, done) {
      const size = this.getTileSize();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const canvas = L.DomUtil.create("canvas", "leaflet-tile");
      canvas.width = size.x * dpr; canvas.height = size.y * dpr;
      canvas.style.width = size.x + "px"; canvas.style.height = size.y + "px";
      const url = L.Util.template(this.options.url, coords);
      this._load(url, 0).then((buf) => {
        if (buf) this._draw(canvas, buf, coords, dpr);
        done(null, canvas);
      }).catch((err) => done(err, canvas));
      return canvas;
    },

    // повтор при 503 (як і для растрового шару)
    async _load(url, attempt) {
      const r = await fetch(url, { credentials: "same-origin" });
      if (r.status === 204) return null;
      if (r.status === 503 && attempt < 3) {
        await new Promise((ok) => setTimeout(ok, [2000, 6000, 15000][attempt] + Math.random() * 1500));
        return this._load(url, attempt + 1);
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return new Uint8Array(await r.arrayBuffer());
    },

    _draw(canvas, buf, coords, dpr) {
      const lay = decode(buf)[this.options.layer];
      if (!lay) return;
      const ctx = canvas.getContext("2d");
      const k = (canvas.width) / lay.extent;
      ctx.lineWidth = this.options.weight * dpr;
      ctx.strokeStyle = this.options.color;
      ctx.lineJoin = "round";
      ctx.beginPath();
      const labels = [];
      for (const f of lay.features) {
        if (f.type !== 3) continue;
        let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
        for (const r of f.rings) {
          r.forEach(([x, y], i) => {
            const px = x * k, py = y * k;
            i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
            if (x < minx) minx = x; if (x > maxx) maxx = x; if (y < miny) miny = y; if (y > maxy) maxy = y;
          });
        }
        if (coords.z >= this.options.labelZoom && f.props.cadnum)
          labels.push({ t: f.props.cadnum, x: (minx + maxx) / 2 * k, y: (miny + maxy) / 2 * k, w: (maxx - minx) * k });
      }
      ctx.stroke();
      // номери — лише там, де ділянка достатньо широка і центр усередині тайла
      if (labels.length) {
        const fs = 10.5 * dpr;
        ctx.font = `600 ${fs}px system-ui, sans-serif`;
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.lineWidth = 3 * dpr; ctx.strokeStyle = this.options.labelHalo; ctx.fillStyle = this.options.labelColor;
        for (const l of labels) {
          const tw = ctx.measureText(l.t).width;
          // підпис має вміститися і в ділянку, і повністю в тайл (інакше його обріже край)
          if (tw > l.w * 0.95) continue;
          if (l.x - tw / 2 < 2 || l.x + tw / 2 > canvas.width - 2 || l.y - fs < 0 || l.y + fs > canvas.height) continue;
          ctx.strokeText(l.t, l.x, l.y); ctx.fillText(l.t, l.x, l.y);
        }
      }
    },
  });
  L.vectorParcels = (opts) => new L.VectorParcels(opts);
  window.KDMVT = { decode };
})();
