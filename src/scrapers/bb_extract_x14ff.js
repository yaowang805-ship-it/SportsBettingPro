/**
 * BB体育 (pc.x14ff.com) 通用提取器
 *
 * 解析 pc.x14ff.com SPA 的 document.body.innerText 布局：
 *   联赛名
 *   独赢/胜负 让球/让分 大小       ← 市场类型标记
 *   mm/dd hh:mm                    ← 比赛时间
 *   球队A                           ← 主队
 *   球队B                           ← 客队
 *   和                              ← 仅足球有（平局标记）
 *   ID号                           ← 比赛ID（数字）
 *   主/客                           ← 胜负方向的下一行是赔率
 *   X.XX [ODDS]                    ← 赔率值
 *   +/-X.X                         ← 让球盘口线
 *   大/小 X.X                      ← 大小球盘口线
 */

(function() {
    var lines = document.body.innerText.split('\n').filter(Boolean);
    var results = [];
    var currentLeague = '';
    var i = 0;

    // 纯标点行（仅含括号和空格）
    var PUNCT_ONLY = /^[（(）)\s]+$/;
    // 时间格式：mm/dd hh:mm 或 hh:mm
    var TIME_RE = /^\d{1,2}\/\d{1,2}\s+\d{1,2}:\d{2}$/;
    var TIME_RE2 = /^\d{1,2}:\d{2}$/;
    // 赔率格式：X.XX 或 X.XXXX
    var ODD_RE = /^\d+\.\d{2,4}$/;
    // 盘口线：+/-X.X 或 +/-X/X 或 大/小 X.X
    var LINE_RE = /^[+-]/;
    var OU_LINE_RE = /^[大小]/;
    // 市场类型标记行
    var MARKET_RE = /^(?:独赢|让球|大小|胜负|让分)\s/;
    // 纯数字行（比赛ID）
    var NUM_RE = /^\d+$/;
    // 联赛关键词行（包含这些词的可能是联赛名）
    var LEAGUE_INDICATORS = ['联赛', '杯', '赛', 'NBA', 'WNBA', 'NBL', 'MLB', 'NPB', 'ATP', 'WTA', '冠军', '锦标', '巡回', '公开赛'];
    // 跳过词
    var SKIP_WORDS = ['更多', '波胆', '15分钟', 'arrow', '体育菜单', '热门赛事', '设置', '账户', '搜索', '投注单',
                      '我的关注', '猜你喜欢', '未结算', '滚球', '今日', '早盘', '欧洲盘', '简体中文', '英文',
                      '时间联赛', '赛事筛选', '体育赛事', '注单历史', '赛果', '体育规则', '投注教程',
                      '全部', '世界杯', 'LIVE', '已经到底了'];

    function isSkipWord(line) {
        for (var s = 0; s < SKIP_WORDS.length; s++) {
            if (line.indexOf(SKIP_WORDS[s]) >= 0) return true;
        }
        return false;
    }

    function isLeagueHeader(line) {
        if (MARKET_RE.test(line)) return false;
        if (ODD_RE.test(line)) return false;
        if (LINE_RE.test(line)) return false;
        if (OU_LINE_RE.test(line)) return false;
        if (TIME_RE.test(line)) return false;
        if (TIME_RE2.test(line)) return false;
        if (NUM_RE.test(line)) return false;
        if (PUNCT_ONLY.test(line)) return false;
        if (isSkipWord(line)) return false;
        if (line.length < 2) return false;
        // 如果下一行是市场类型标记，则是联赛头
        if (i + 1 < lines.length && MARKET_RE.test(lines[i + 1])) return true;
        // 包含联赛关键词
        for (var l = 0; l < LEAGUE_INDICATORS.length; l++) {
            if (line.indexOf(LEAGUE_INDICATORS[l]) >= 0) return true;
        }
        return false;
    }

    function looksLikeTeam(line) {
        if (ODD_RE.test(line)) return false;
        if (LINE_RE.test(line)) return false;
        if (OU_LINE_RE.test(line)) return false;
        if (TIME_RE.test(line)) return false;
        if (TIME_RE2.test(line)) return false;
        if (NUM_RE.test(line)) return false;
        if (PUNCT_ONLY.test(line)) return false;
        if (isSkipWord(line)) return false;
        if (line === '和') return false;
        if (line === '主' || line === '客') return false;
        if (line.length < 2) return false;
        return true;
    }

    while (i < lines.length) {
        var line = lines[i].trim();
        i++;

        if (!line) continue;
        if (isSkipWord(line)) continue;

        // 检测联赛头
        if (isLeagueHeader(line)) {
            currentLeague = line;
            continue;
        }

        // 检测比赛：找到时间行
        if (TIME_RE.test(line) || TIME_RE2.test(line)) {
            var matchTime = line;

            // 消费掉市场类型标记行（独赢 让球 大小 等）
            while (i < lines.length && MARKET_RE.test(lines[i].trim())) {
                i++;
            }

            // 如果当前行看起来是市场标记，跳过
            // 接下来应该是 主队 → 客队 → [和] → [ID] → 赔率+盘口
            var home = '';
            var away = '';
            var hasDraw = false;

            // 收集后续所有非赔率、非盘口文本行作为队名
            var collected = [];
            while (i < lines.length) {
                var cl = lines[i].trim();
                if (!cl) { i++; continue; }
                if (ODD_RE.test(cl)) break;
                if (LINE_RE.test(cl)) break;
                if (OU_LINE_RE.test(cl)) break;
                if (TIME_RE.test(cl)) break; // 新比赛开始
                if (isLeagueHeader(lines[i].trim())) break; // 新联赛开始
                collected.push(cl);
                i++;
            }

            // 解析收集到的行：home → away → [和] → [number]
            var idx = 0;
            while (idx < collected.length) {
                var cl = collected[idx];
                if (cl === '主' || cl === '客' || NUM_RE.test(cl) || cl === '和' || PUNCT_ONLY.test(cl) || isSkipWord(cl)) {
                    idx++;
                    continue;
                }
                break;
            }

            if (idx < collected.length) home = collected[idx];
            idx++;
            // 跳过 和
            if (idx < collected.length && collected[idx] === '和') {
                hasDraw = true;
                idx++;
            }
            if (idx < collected.length && NUM_RE.test(collected[idx])) idx++; // 跳过 ID
            if (idx < collected.length && !away) away = collected[idx];

            // 跳过 和 和 ID 后的剩余行
            idx++;
            while (idx < collected.length && (collected[idx] === '和' || NUM_RE.test(collected[idx]) || PUNCT_ONLY.test(collected[idx]) || isSkipWord(collected[idx]))) {
                idx++;
            }
            if (!away && idx < collected.length) away = collected[idx];

            if (!home || !away) continue;

            // 现在收集赔率（从当前位置往后）
            var oddsValues = [];
            var fullTextLines = [matchTime, home, away];
            if (hasDraw) fullTextLines.push('和');

            while (i < lines.length) {
                var ol = lines[i].trim();
                if (!ol) { i++; continue; }
                // 新联赛或新比赛开始
                if (TIME_RE.test(ol)) break;
                if (isLeagueHeader(ol)) break;
                if (ol.indexOf('更多球线') >= 0 || ol.indexOf('波胆') >= 0 || ol.indexOf('15分钟') >= 0) { i++; continue; }
                if (ODD_RE.test(ol)) {
                    oddsValues.push(ol);
                } else if (LINE_RE.test(ol) || OU_LINE_RE.test(ol)) {
                    // 盘口线，保留
                } else if (ol === '主' || ol === '客' || ol === '和') {
                    // 方向标签，跳过
                } else if (NUM_RE.test(ol)) {
                    // 数字，可能是比赛ID或其他
                } else if (isSkipWord(ol)) {
                    // 跳过词
                } else {
                    // 未知内容，可能是下一个联赛/比赛的开头
                    break;
                }
                fullTextLines.push(ol);
                i++;
            }

            if (oddsValues.length >= 2) {
                var parts = matchTime.split(/\s+/);
                var period = parts.length >= 2 ? parts[0] : '';
                var timeVal = parts.length >= 2 ? parts[1] : matchTime;
                results.push({
                    league: currentLeague,
                    home: home,
                    away: away,
                    period: period,
                    time: timeVal,
                    odds_values: oddsValues,
                    full_text: fullTextLines.join('\n'),
                });
            }
        }
    }

    return JSON.stringify(results);
})();
