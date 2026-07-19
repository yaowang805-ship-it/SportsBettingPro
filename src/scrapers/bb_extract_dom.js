/**
 * BB体育 (pc.x14ff.com) DOM 提取器
 *
 * 从 DOM 结构按 class name 读取赔率，而非解析 body.innerText。
 * 可同时提取 FT (match-full-odds-draw/handicap/total) 和
 * HT (match-full-odds-drawHT/handicapHT/totalHT) 盘口。
 *
 * 输出字段兼容文本提取器（odds_values, full_text），同时增加
 * 结构化 odds_ft / odds_ht 字段。
 *
 * 返回 [{ league, home, away, period, time, odds_values, full_text,
 *          odds_ft: { ml:[], handicap:{...}, total:{...} },
 *          odds_ht: { ml:[], handicap:{...}, total:{...} },
 *        }]
 */
(function() {
    var matches = document.querySelectorAll('.home-match-list__item');
    var results = [];

    /** 解析亚洲让球线： "-0/0.5" → -0.25, "+0.5/1" → 0.75, "-0.5" → -0.5 */
    function parseAsianLine(str) {
        if (!str) return null;
        str = str.trim();
        var neg = str.startsWith('-');
        var num = str.replace(/^[+\-]/, '');
        if (num.indexOf('/') >= 0) {
            var parts = num.split('/');
            var a = parseFloat(parts[0]);
            var b = parseFloat(parts[1]);
            if (isNaN(a) || isNaN(b)) return null;
            var avg = (a + b) / 2;
            return neg ? -avg : avg;
        }
        var v = parseFloat(num);
        return isNaN(v) ? null : (neg ? -v : v);
    }

    matches.forEach(function(el) {
        try {
            var timeEl = el.querySelector('.match-left-text');
            var timeVal = el.querySelector('.match-left-time');
            var period = timeEl ? timeEl.innerText.trim() : '';
            var time = timeVal ? timeVal.innerText.trim() : '';

            var teamEls = el.querySelectorAll('.team-name.team-score');
            var home = teamEls.length >= 1 ? teamEls[0].innerText.trim() : '';
            var away = teamEls.length >= 2 ? teamEls[1].innerText.trim() : '';
            if (!home || !away) return;

            var league = '';
            var group = el.closest('.group-matches');
            if (group) {
                var ln = group.querySelector('.league-name');
                if (ln) league = ln.innerText.trim();
            }

            function readOddsBox(className) {
                var box = el.querySelector('.' + className);
                if (!box) return null;
                var vals = [];
                box.querySelectorAll('.value').forEach(function(v) { vals.push(parseFloat(v.innerText.trim()) || 0); });
                var prefixes = [];
                box.querySelectorAll('.prefix-text span').forEach(function(p) { prefixes.push(p.innerText.trim()); });
                return { values: vals, prefixes: prefixes };
            }

            function parseHandicap(boxData) {
                if (!boxData || boxData.values.length < 2) return null;
                var l1 = (boxData.prefixes.length >= 1 ? boxData.prefixes[0] : '').trim();
                var l2 = (boxData.prefixes.length >= 2 ? boxData.prefixes[1] : '').trim();
                if (!l1 && !l2) return null;
                var hl = parseAsianLine(l1);
                var al = parseAsianLine(l2);
                if (hl === null && al === null) return null;
                return {
                    home_line: hl,
                    away_line: al,
                    home_line_str: l1,
                    away_line_str: l2,
                    home_odds: boxData.values[0],
                    away_odds: boxData.values.length >= 2 ? boxData.values[1] : null,
                };
            }

            function parseTotal(boxData) {
                if (!boxData || boxData.values.length < 2) return null;
                var lineStr = (boxData.prefixes.length >= 1 ? boxData.prefixes[0] : '').trim();
                var lineNum = null;
                if (lineStr) {
                    // Extract number from "大 2/2.5" or "小 0.5/1" or "大 10.5"
                    var m = lineStr.match(/(\d+(?:\.\d+)?(?:\/\d+(?:\.\d+)?)?)/);
                    if (m) {
                        if (m[1].indexOf('/') >= 0) {
                            var parts = m[1].split('/');
                            lineNum = (parseFloat(parts[0]) + parseFloat(parts[1])) / 2;
                        } else {
                            lineNum = parseFloat(m[1]);
                        }
                    }
                }
                return {
                    line: lineNum,
                    line_str: lineStr,
                    over_odds: boxData.values[0],
                    under_odds: boxData.values[1],
                };
            }

            // Read odds boxes dynamically:
            // - Tennis/baseball use 'match-full-odds-tennisDraw' instead of 'match-full-odds-draw'
            // - Tennis uses 'handicap1' (games handicap) instead of 'handicap' (which is null)
            var ml = readOddsBox('match-full-odds-tennisDraw') || readOddsBox('match-full-odds-draw');
            var hc = readOddsBox('match-full-odds-handicap') || readOddsBox('match-full-odds-handicap1');
            var ou = readOddsBox('match-full-odds-total');
            var mlHT = readOddsBox('match-full-odds-drawHT');
            var hcHT = readOddsBox('match-full-odds-handicapHT');
            var ouHT = readOddsBox('match-full-odds-totalHT');

            var oddsFt = { ml: ml ? ml.values : [] };
            var oddsHt = { ml: mlHT ? mlHT.values : [] };

            var hcParsed = parseHandicap(hc);
            if (hcParsed) oddsFt.handicap = hcParsed;
            var ouParsed = parseTotal(ou);
            if (ouParsed) oddsFt.total = ouParsed;

            var hcHTParsed = parseHandicap(hcHT);
            if (hcHTParsed) oddsHt.handicap = hcHTParsed;
            var ouHTParsed = parseTotal(ouHT);
            if (ouHTParsed) oddsHt.total = ouHTParsed;

            // Build backward-compatible odds_values (FT order: ML 3 + HC 2 + OU 2)
            var oddsVals = [];
            if (ml) ml.values.forEach(function(v) { oddsVals.push(String(v)); });
            if (hc) hc.values.forEach(function(v) { oddsVals.push(String(v)); });
            if (ou) ou.values.forEach(function(v) { oddsVals.push(String(v)); });

            // Build full_text for backward compatibility
            var ftLines = [period + ' ' + time, home, away];
            if (ml) {
                ml.values.forEach(function(v) { ftLines.push(String(v)); });
            }
            if (hc) {
                hc.prefixes.forEach(function(p) { ftLines.push(p); });
                hc.values.forEach(function(v) { ftLines.push(String(v)); });
            }
            if (ou) {
                ou.prefixes.forEach(function(p) { ftLines.push(p); });
                ou.values.forEach(function(v) { ftLines.push(String(v)); });
            }

            results.push({
                league: league,
                home: home,
                away: away,
                period: period,
                time: time,
                odds_values: oddsVals,
                full_text: ftLines.join('\n'),
                odds_ft: oddsFt,
                odds_ht: oddsHt,
            });
        } catch(e) {
            // Skip malformed entries
        }
    });

    return JSON.stringify(results);
})();
