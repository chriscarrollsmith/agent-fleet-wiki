<?php
// Remote half of deploy/rotate-credentials.sh. Copied to the instance and run there.
//
//   sudo php rotate-bookstack.php s3-env /tmp/bookstack-s3.env
//   sudo -u www-data php rotate-bookstack.php s3-check AKIA...
//   sudo -u www-data php rotate-bookstack.php users /tmp/users.json
//
// users prints one JSON object per account on stdout. Secrets are not printed.

declare(strict_types=1);

const BOOKSTACK = '/var/www/bookstack';

$command = $argv[1] ?? '';
$argument = $argv[2] ?? '';

if ($command === 's3-env') {
    s3_env($argument);
} elseif ($command === 's3-check') {
    boot();
    s3_check($argument);
} elseif ($command === 'users') {
    boot();
    rotate_users($argument);
} else {
    fwrite(STDERR, "usage: rotate-bookstack.php s3-env|s3-check|users <path-or-key-id>\n");
    exit(1);
}

function boot(): void
{
    chdir(BOOKSTACK);
    require BOOKSTACK . '/vendor/autoload.php';
    $app = require BOOKSTACK . '/bootstrap/app.php';
    $app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
}

function s3_env(string $path): void
{
    if ($path === '' || !is_file($path)) {
        fwrite(STDERR, "s3 env file is missing\n");
        exit(1);
    }
    $new = [];
    foreach (file($path, FILE_IGNORE_NEW_LINES) as $line) {
        if ($line === '' || str_starts_with($line, '#')) {
            continue;
        }
        $parts = explode('=', $line, 2);
        if (count($parts) !== 2 || ($parts[0] !== 'STORAGE_S3_KEY' && $parts[0] !== 'STORAGE_S3_SECRET')) {
            fwrite(STDERR, "s3 env file must contain only STORAGE_S3_KEY and STORAGE_S3_SECRET\n");
            exit(1);
        }
        $new[$parts[0]] = $parts[1];
    }
    foreach (['STORAGE_S3_KEY', 'STORAGE_S3_SECRET'] as $key) {
        if (!isset($new[$key]) || $new[$key] === '') {
            fwrite(STDERR, "s3 env file is missing {$key}\n");
            exit(1);
        }
    }
    $envPath = BOOKSTACK . '/.env';
    $lines = file($envPath, FILE_IGNORE_NEW_LINES);
    $out = [];
    $seen = [];
    foreach ($lines as $line) {
        $key = explode('=', $line, 2)[0];
        if (isset($new[$key])) {
            $out[] = $key . '=' . $new[$key];
            $seen[$key] = true;
        } else {
            $out[] = $line;
        }
    }
    foreach (array_keys($new) as $key) {
        if (!isset($seen[$key])) {
            fwrite(STDERR, "server env is missing {$key}\n");
            exit(1);
        }
    }
    file_put_contents($envPath, implode("\n", $out) . "\n");
    chmod($envPath, 0740);
    echo 'server_key_id ' . $new['STORAGE_S3_KEY'] . "\n";
}

function s3_check(string $expected): void
{
    $actual = (string) config('filesystems.disks.s3.key');
    if ($expected === '' || $actual !== $expected) {
        fwrite(STDERR, "app s3 key id does not match the new key\n");
        exit(1);
    }
    try {
        Illuminate\Support\Facades\Storage::disk('s3')->exists('uploads/files/rotation-probe-does-not-exist');
    } catch (Throwable $e) {
        fwrite(STDERR, 's3 app check failed ' . get_class($e) . "\n");
        exit(1);
    }
    echo "s3_app_ok\n";
}

function rotate_users(string $path): void
{
    if ($path === '' || !is_file($path)) {
        fwrite(STDERR, "users file is missing\n");
        exit(1);
    }
    $payload = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
    if (!is_array($payload['users'] ?? null)) {
        fwrite(STDERR, "users file has no users array\n");
        exit(1);
    }
    $repo = app(BookStack\Users\UserRepo::class);
    foreach ($payload['users'] as $item) {
        if (!is_array($item)) {
            echo json_encode(['ok' => false, 'email' => '', 'error' => 'user entry was not an object']) . "\n";
            continue;
        }
        rotate_one($repo, $item);
        fflush(STDOUT);
    }
}

function rotate_one(BookStack\Users\UserRepo $repo, array $item): void
{
    $email = (string) ($item['email'] ?? '');
    $created = null;
    $committed = false;
    try {
        $user = find_user($email, (int) ($item['user_id'] ?? 0));
        if (!empty($item['rotate_token'])) {
            $tokenId = (string) ($item['token_id'] ?? '');
            $tokenSecret = (string) ($item['token_secret'] ?? '');
            if ($tokenId === '' || $tokenSecret === '') {
                throw new RuntimeException('token id and secret are required');
            }
            if (BookStack\Api\ApiToken::query()->where('token_id', $tokenId)->exists()) {
                throw new RuntimeException('token id collision');
            }
            $created = (new BookStack\Api\ApiToken())->forceFill([
                'name' => 'fleet',
                'token_id' => $tokenId,
                'secret' => Illuminate\Support\Facades\Hash::make($tokenSecret),
                'user_id' => $user->id,
                'expires_at' => BookStack\Api\ApiToken::defaultExpiry(),
            ]);
            $created->save();
            $code = verify_token($tokenId, $tokenSecret);
            if ($code !== 200) {
                throw new RuntimeException('token verify http ' . $code);
            }
        }
        $password = (string) ($item['password'] ?? '');
        if ($password === '') {
            throw new RuntimeException('password is empty');
        }
        $repo->updateWithoutActivity($user, ['password' => $password], true);
        $user->remember_token = Illuminate\Support\Str::random(60);
        $user->save();
        if (!Illuminate\Support\Facades\Hash::check($password, (string) $user->fresh()->password)) {
            throw new RuntimeException('password check failed');
        }
        $committed = true;
        $removed = 0;
        if ($created !== null) {
            $old = BookStack\Api\ApiToken::query()->where('user_id', $user->id)->where('id', '!=', $created->id)->get();
            foreach ($old as $token) {
                $token->delete();
                $removed++;
            }
        }
        $created = null;
        echo json_encode([
            'ok' => true,
            'email' => $user->email,
            'name' => $user->name,
            'user_id' => $user->id,
            'removed_tokens' => $removed,
            'sessions' => clear_sessions((int) $user->id),
        ]) . "\n";
    } catch (Throwable $e) {
        if ($committed) {
            echo json_encode([
                'ok' => true,
                'email' => $email,
                'name' => $email,
                'user_id' => (int) ($item['user_id'] ?? 0),
                'removed_tokens' => 0,
                'sessions' => 0,
            ]) . "\n";
            return;
        }
        if ($created !== null && $created->exists) {
            $created->delete();
        }
        echo json_encode(['ok' => false, 'email' => $email, 'error' => $e->getMessage()]) . "\n";
    }
}

function find_user(string $email, int $userId): BookStack\Users\Models\User
{
    if ($email === '') {
        throw new RuntimeException('email is empty');
    }
    if ($userId > 0) {
        $user = BookStack\Users\Models\User::query()->find($userId);
        if ($user === null || strcasecmp((string) $user->email, $email) !== 0) {
            throw new RuntimeException('bookstack user does not match the requested email');
        }
        return $user;
    }
    $user = BookStack\Users\Models\User::query()->whereRaw('LOWER(email) = ?', [strtolower($email)])->first();
    if ($user === null) {
        throw new RuntimeException('bookstack user was not found');
    }
    return $user;
}

function verify_token(string $tokenId, string $secret): int
{
    $url = rtrim((string) config('app.url'), '/') . '/api/system';
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_HTTPHEADER => ['Authorization: Token ' . $tokenId . ':' . $secret, 'Accept: application/json'],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 30,
    ]);
    curl_exec($ch);
    $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return $code;
}

function clear_sessions(int $userId): int
{
    $deleted = 0;
    $driver = (string) config('session.driver');
    if ($driver === 'database') {
        $table = (string) config('session.table', 'sessions');
        return Illuminate\Support\Facades\DB::table($table)->where('user_id', $userId)->delete();
    }
    if ($driver !== 'file') {
        return 0;
    }
    $dir = storage_path('framework/sessions');
    foreach (scandir($dir) ?: [] as $name) {
        if ($name === '.' || $name === '..' || $name[0] === '.') {
            continue;
        }
        $file = $dir . '/' . $name;
        if (!is_file($file)) {
            continue;
        }
        $data = @unserialize((string) file_get_contents($file), ['allowed_classes' => false]);
        if (!is_array($data)) {
            continue;
        }
        foreach ($data as $key => $value) {
            if (is_string($key) && str_starts_with($key, 'login_web_') && (int) $value === $userId) {
                unlink($file);
                $deleted++;
                break;
            }
        }
    }
    return $deleted;
}
