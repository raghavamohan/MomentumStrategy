/** Pure bar / time / indicator helpers (no chart instances). */



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


