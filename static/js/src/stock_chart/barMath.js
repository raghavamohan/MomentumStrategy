/** Pure bar / time / indicator helpers (no chart instances). */

export function calcSMA(bars, period) {
  var out = [];
  for (var i = 0; i < bars.length; i++) {
    if (i < period - 1) continue;
    var sum = 0;
    for (var j = i - period + 1; j <= i; j++) sum += bars[j].close;
    out.push({ time: bars[i].time, value: sum / period });
  }
  return out;
}

export function calcEMA(bars, period) {
  var out = [], k = 2 / (period + 1), ema = 0, started = false;
  for (var i = 0; i < bars.length; i++) {
    if (i < period - 1) continue;
    if (!started) {
      var s = 0;
      for (var j = 0; j < period; j++) s += bars[j].close;
      ema = s / period;
      started = true;
    } else ema = bars[i].close * k + ema * (1 - k);
    out.push({ time: bars[i].time, value: ema });
  }
  return out;
}

/**
 * RSI(period) with leading whitespace so the RSI series has one slot per
 * candle bar. Same logical bar count + same `rightOffset` lets the two
 * charts share a 1:1 visible-logical-range mirror with no clamping.
 */
export function calcRSI(bars, period) {
  var out = [];
  var prefixCount = Math.min(period, bars.length);
  for (var k = 0; k < prefixCount; k++) {
    out.push({ time: bars[k].time });
  }
  if (bars.length < period + 1) return out;
  var gains = 0, losses = 0;
  for (var i = 1; i <= period; i++) {
    var d = bars[i].close - bars[i - 1].close;
    if (d >= 0) gains += d; else losses -= d;
  }
  var ag = gains / period, al = losses / period;
  out.push({ time: bars[period].time, value: al === 0 ? 100 : 100 - 100 / (1 + ag / al) });
  for (var i = period + 1; i < bars.length; i++) {
    var d2 = bars[i].close - bars[i - 1].close;
    ag = (ag * (period - 1) + Math.max(d2, 0)) / period;
    al = (al * (period - 1) + Math.max(-d2, 0)) / period;
    out.push({ time: bars[i].time, value: al === 0 ? 100 : 100 - 100 / (1 + ag / al) });
  }
  return out;
}

/** UTC ms for a bar's session (daily string uses noon IST). */
export function barToUtcMs(b) {
  if (!b || b.time == null) return 0;
  if (typeof b.time === 'number') return b.time * 1000;
  if (typeof b.time === 'string') {
    if (b.time.length === 10) return new Date(b.time + 'T12:00:00+05:30').getTime();
    return new Date(b.time).getTime();
  }
  return 0;
}

/**
 * Week bucket id: Monday (IST) of the week containing the bar, as YYYY-MM-DD.
 * Uses Asia/Kolkata civil dates only (no browser-local getDay / toISOString).
 */
export function istMondayWeekKeyFromBar(b) {
  var utcMs = barToUtcMs(b);
  if (!utcMs) return String(b.time);
  var dayStr = istDayString(utcMs);
  var parts = dayStr.split('-');
  var y = +parts[0], mo = +parts[1], da = +parts[2];
  var noonMs = new Date(
    y + '-' + String(mo).padStart(2, '0') + '-' + String(da).padStart(2, '0') + 'T12:00:00+05:30'
  ).getTime();
  var wdStr = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    weekday: 'short'
  }).format(new Date(noonMs));
  var wdMap = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  var wd = wdMap[wdStr];
  if (wd === undefined) return dayStr;
  var daysFromMon = (wd + 6) % 7;
  var monMs = noonMs - daysFromMon * 86400000;
  return istDayString(monMs);
}

export function aggregateBars(bars, mode) {
  var buckets = {}, order = [];
  bars.forEach(function (b) {
    var t = typeof b.time === 'number' ? b.time : (new Date(b.time)).getTime() / 1000;
    var d = new Date(t * 1000);
    var key;
    if (mode === 'week') {
      key = istMondayWeekKeyFromBar(b);
    } else {
      key = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-01';
    }
    if (!buckets[key]) {
      buckets[key] = { time: key, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume };
      order.push(key);
    } else {
      var bk = buckets[key];
      bk.high = Math.max(bk.high, b.high);
      bk.low = Math.min(bk.low, b.low);
      bk.close = b.close;
      bk.volume += b.volume;
    }
  });
  return order.map(function (k) { return buckets[k]; });
}

export function toTime(dateStr) {
  if (!dateStr) return 0;
  if (dateStr.length === 10) return dateStr;
  return Math.floor(new Date(dateStr).getTime() / 1000);
}

export function barStepSec(iv) {
  if (iv === 'minute') return 60;
  if (iv === '5minute') return 300;
  if (iv === '15minute') return 900;
  if (iv === '60minute') return 3600;
  return null;
}

export function istDayString(tsMs) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit'
  }).format(new Date(tsMs));
}

/** IST YYYY-MM-DD for a bar `time` (daily string, unix sec, or parseable datetime). */
export function istDayKeyForBar(b) {
  if (!b || b.time == null) return '';
  var t = b.time;
  if (typeof t === 'string' && t.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(t)) return t;
  if (typeof t === 'number' && isFinite(t)) return istDayString(t * 1000);
  if (typeof t === 'string') {
    var ms = new Date(t).getTime();
    if (isFinite(ms)) return istDayString(ms);
  }
  var utc = barToUtcMs(b);
  return utc ? istDayString(utc) : '';
}

export function liveBarTimeToSec(t) {
  if (typeof t === 'number') return t;
  return Math.floor(new Date(t + 'T12:00:00+05:30').getTime() / 1000);
}

/** Unix seconds for any chart / trendline time (LW UTCTimestamp, daily string, etc.). */
export function chartTimeToUnixSec(t) {
  if (t == null) return NaN;
  if (typeof t === 'number' && isFinite(t)) return t;
  if (typeof t === 'string' && t.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(t)) {
    return Math.floor(new Date(t + 'T12:00:00+05:30').getTime() / 1000);
  }
  var ms = new Date(t).getTime();
  return isFinite(ms) ? Math.floor(ms / 1000) : NaN;
}

/** Match stored time shape to the reference bar (daily string vs unix seconds). */
export function unixSecToChartTime(sec, refBar) {
  var t = refBar && refBar.time;
  if (typeof t === 'string' && t.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(t)) {
    return istDayString(sec * 1000);
  }
  return Math.floor(sec);
}
