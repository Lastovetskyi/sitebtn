<?php
// Лічильник відвідувань BTN. Пише рядок у файл поза каталогом сайту.
// Викликається картинкою-маячком із кожної сторінки; боти JS не виконують,
// тому сюди потрапляють переважно люди.

$clean = function ($v, $max = 300) {
    $v = is_string($v) ? $v : '';
    $v = str_replace(array("\t", "\r", "\n"), ' ', $v);
    return mb_substr($v, 0, $max);
};

$ua = isset($_SERVER['HTTP_USER_AGENT']) ? $_SERVER['HTTP_USER_AGENT'] : '';
$low = strtolower($ua);
$isBot = (strpos($low, 'bot') !== false) || (strpos($low, 'crawler') !== false)
      || (strpos($low, 'spider') !== false) || (strpos($low, 'headless') !== false)
      || $ua === '';

if (!$isBot) {
    $ip = isset($_SERVER['HTTP_X_FORWARDED_FOR']) && $_SERVER['HTTP_X_FORWARDED_FOR'] !== ''
        ? trim(explode(',', $_SERVER['HTTP_X_FORWARDED_FOR'])[0])
        : (isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '');

    $ref = $clean(isset($_GET['r']) ? $_GET['r'] : '');
    $host = '';
    if ($ref !== '') {
        $parts = @parse_url($ref);
        if (!empty($parts['host'])) {
            $host = $parts['host'];
        }
    }

    $line = implode("\t", array(
        gmdate('Y-m-d H:i:s'),
        $clean($ip, 45),
        $clean(isset($_GET['p']) ? $_GET['p'] : '/', 200),
        $host,
        $clean($ua, 200),
    )) . "\n";

    @file_put_contents(__DIR__ . '/../btn-stats.tsv', $line, FILE_APPEND | LOCK_EX);
}

header('Content-Type: image/gif');
header('Cache-Control: no-store, no-cache, must-revalidate');
echo base64_decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7');
