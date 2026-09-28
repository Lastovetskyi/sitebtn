<?php
// Панель статистики BTN. Відкривається лише за таємним ключем у адресі.
$TOKEN = '4892881a6cf6045d';

if (!isset($_GET['k']) || !hash_equals($TOKEN, (string)$_GET['k'])) {
    http_response_code(404);
    echo 'Not found';
    exit;
}

$days = isset($_GET['d']) ? max(1, min(365, (int)$_GET['d'])) : 30;
$file = __DIR__ . '/../btn-stats.tsv';
$rows = array();
$since = gmdate('Y-m-d', time() - $days * 86400);

if (is_readable($file)) {
    $fh = fopen($file, 'r');
    while (($line = fgets($fh)) !== false) {
        $p = explode("\t", rtrim($line, "\n"));
        if (count($p) < 5) { continue; }
        if (substr($p[0], 0, 10) < $since) { continue; }
        $rows[] = $p;
    }
    fclose($fh);
}

$byDay = array(); $pages = array(); $refs = array(); $ipsByDay = array(); $allIps = array();
foreach ($rows as $r) {
    $d = substr($r[0], 0, 10);
    $byDay[$d] = isset($byDay[$d]) ? $byDay[$d] + 1 : 1;
    $ipsByDay[$d][$r[1]] = true;
    $allIps[$r[1]] = true;
    $pages[$r[2]] = isset($pages[$r[2]]) ? $pages[$r[2]] + 1 : 1;
    if ($r[3] !== '') { $refs[$r[3]] = isset($refs[$r[3]]) ? $refs[$r[3]] + 1 : 1; }
}
krsort($byDay); arsort($pages); arsort($refs);

// --- країни: визначаємо через ip-api.com, результат кешуємо ---
$geoFile = __DIR__ . '/../btn-geo.json';
$geo = array();
if (is_readable($geoFile)) {
    $tmp = json_decode((string)file_get_contents($geoFile), true);
    if (is_array($tmp)) { $geo = $tmp; }
}
$todo = array();
foreach (array_keys($allIps) as $ip) {
    if (!isset($geo[$ip]) && filter_var($ip, FILTER_VALIDATE_IP)) { $todo[] = $ip; }
}
$todo = array_slice($todo, 0, 100);
if (count($todo) > 0) {
    $ctx = stream_context_create(array('http' => array(
        'method' => 'POST',
        'header' => "Content-Type: application/json\r\n",
        'content' => json_encode(array_map(function ($ip) { return array('query' => $ip, 'fields' => 'country,query'); }, $todo)),
        'timeout' => 8,
    )));
    $resp = @file_get_contents('http://ip-api.com/batch', false, $ctx);
    if ($resp !== false) {
        $data = json_decode($resp, true);
        if (is_array($data)) {
            foreach ($data as $item) {
                if (isset($item['query'])) {
                    $geo[$item['query']] = isset($item['country']) ? $item['country'] : '—';
                }
            }
            @file_put_contents($geoFile, json_encode($geo), LOCK_EX);
        }
    }
}
$countries = array();
foreach (array_keys($allIps) as $ip) {
    $c = isset($geo[$ip]) ? $geo[$ip] : 'невідомо';
    $countries[$c] = isset($countries[$c]) ? $countries[$c] + 1 : 1;
}
arsort($countries);

$maxDay = 1;
foreach ($byDay as $n) { if ($n > $maxDay) { $maxDay = $n; } }
$AI = array('chatgpt.com', 'chat.openai.com', 'perplexity.ai', 'www.perplexity.ai', 'claude.ai', 'gemini.google.com', 'copilot.microsoft.com');
function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8'); }
?>
<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Статистика BTN</title>
<style>
:root{--bg:#0e1416;--card:#151d20;--ink:#eaf1f2;--muted:#8fa3a8;--line:#222e32;--sun:#e0a05a;--ice:#5fa3b4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;padding:24px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));max-width:1200px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:16px}
.card h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.big{font-size:30px;font-weight:700;color:var(--sun)}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:5px 0;border-bottom:1px solid var(--line);vertical-align:top}
td.n{text-align:right;color:var(--sun);font-variant-numeric:tabular-nums;width:60px}
.bar{height:8px;background:var(--ice);border-radius:2px;min-width:2px}
.ai{color:var(--sun);font-weight:600}
a{color:var(--ice)}
.nav{margin:0 0 18px;font-size:13px}
.nav a{margin-right:12px}
</style>
</head>
<body>
<h1>Статистика btn.co.ua</h1>
<p class="sub">Лише люди: боти сюди не потрапляють, бо не виконують скриптів. Період: <?php echo (int)$days; ?> днів.</p>
<p class="nav">Період:
  <a href="?k=<?php echo h($TOKEN); ?>&d=7">7 днів</a>
  <a href="?k=<?php echo h($TOKEN); ?>&d=30">30 днів</a>
  <a href="?k=<?php echo h($TOKEN); ?>&d=90">90 днів</a>
</p>

<div class="grid">
  <div class="card">
    <h2>Переглядів</h2>
    <div class="big"><?php echo count($rows); ?></div>
  </div>
  <div class="card">
    <h2>Різних відвідувачів</h2>
    <div class="big"><?php echo count($allIps); ?></div>
  </div>
  <div class="card">
    <h2>Переходів із ШІ-сервісів</h2>
    <div class="big"><?php
      $aiHits = 0;
      foreach ($refs as $host => $n) { if (in_array($host, $AI, true)) { $aiHits += $n; } }
      echo $aiHits;
    ?></div>
  </div>
</div>

<div class="grid" style="margin-top:16px">
  <div class="card">
    <h2>По днях</h2>
    <table>
    <?php foreach ($byDay as $d => $n) { $u = isset($ipsByDay[$d]) ? count($ipsByDay[$d]) : 0; ?>
      <tr>
        <td style="width:92px;color:var(--muted)"><?php echo h($d); ?></td>
        <td><div class="bar" style="width:<?php echo (int)round(100 * $n / $maxDay); ?>%"></div></td>
        <td class="n"><?php echo (int)$n; ?></td>
        <td class="n" style="color:var(--muted)"><?php echo (int)$u; ?></td>
      </tr>
    <?php } if (count($byDay) === 0) { echo '<tr><td colspan="4" style="color:var(--muted)">Даних поки немає</td></tr>'; } ?>
    </table>
  </div>

  <div class="card">
    <h2>Сторінки</h2>
    <table>
    <?php $i = 0; foreach ($pages as $p => $n) { if (++$i > 15) { break; } ?>
      <tr><td><a href="<?php echo h($p); ?>"><?php echo h($p); ?></a></td><td class="n"><?php echo (int)$n; ?></td></tr>
    <?php } if (count($pages) === 0) { echo '<tr><td style="color:var(--muted)">—</td></tr>'; } ?>
    </table>
  </div>

  <div class="card">
    <h2>Звідки приходять</h2>
    <table>
    <?php $i = 0; foreach ($refs as $host => $n) { if (++$i > 15) { break; } $cls = in_array($host, $AI, true) ? ' class="ai"' : ''; ?>
      <tr><td<?php echo $cls; ?>><?php echo h($host); ?></td><td class="n"><?php echo (int)$n; ?></td></tr>
    <?php } if (count($refs) === 0) { echo '<tr><td style="color:var(--muted)">Прямі заходи або без реферера</td></tr>'; } ?>
    </table>
  </div>

  <div class="card">
    <h2>Країни</h2>
    <table>
    <?php $i = 0; foreach ($countries as $c => $n) { if (++$i > 15) { break; } ?>
      <tr><td><?php echo h($c); ?></td><td class="n"><?php echo (int)$n; ?></td></tr>
    <?php } if (count($countries) === 0) { echo '<tr><td style="color:var(--muted)">—</td></tr>'; } ?>
    </table>
  </div>
</div>
</body>
</html>
