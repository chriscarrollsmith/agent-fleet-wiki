<?php

/**
 * Create a BookStack user and an API token.
 *
 * Run on the BookStack host, as www-data, from the install directory's
 * vendor autoload. Arguments:
 *   php create-user.php /var/www/bookstack /path/to/payload.json
 *
 * The JSON payload is {"name","email","password","role"}.
 * Stdout is key=value lines. The token secret is printed once.
 */

declare(strict_types=1);

ini_set('display_errors', '0');

if ($argc !== 3) {
    fwrite(STDERR, "Usage: php create-user.php /var/www/bookstack /path/to/payload.json\n");
    exit(1);
}

$appRoot = $argv[1];
$payloadPath = $argv[2];

if (!is_file($appRoot . '/vendor/autoload.php') || !is_file($appRoot . '/bootstrap/app.php')) {
    fwrite(STDERR, "BookStack install not found at {$appRoot}\n");
    exit(1);
}

$payload = json_decode((string) file_get_contents($payloadPath), true);
if (!is_array($payload)) {
    fwrite(STDERR, "Could not read the user payload.\n");
    exit(1);
}

foreach (['name', 'email', 'password', 'role'] as $key) {
    if (!isset($payload[$key]) || !is_string($payload[$key]) || $payload[$key] === '') {
        fwrite(STDERR, "Payload is missing {$key}.\n");
        exit(1);
    }
}

try {
    chdir($appRoot);
    require $appRoot . '/vendor/autoload.php';
    $app = require $appRoot . '/bootstrap/app.php';
    $kernel = $app->make(Illuminate\Contracts\Console\Kernel::class);
    $kernel->bootstrap();

    $result = create_bookstack_user(
        $payload['name'],
        $payload['email'],
        $payload['password'],
        $payload['role']
    );
} catch (Throwable $e) {
    fwrite(STDERR, "BookStack user creation failed: {$e->getMessage()}\n");
    exit(1);
}

foreach ($result as $key => $value) {
    fwrite(STDOUT, $key . '=' . $value . "\n");
}

/**
 * @return array<string, string>
 */
function create_bookstack_user(string $name, string $email, string $password, string $roleName): array
{
    $admin = BookStack\Users\Models\Role::getSystemRole('admin')?->users()->first();
    if ($admin) {
        auth()->login($admin);
    }

    $email = mb_strtolower(trim($email));
    $existing = BookStack\Users\Models\User::query()
        ->whereRaw('LOWER(email) = ?', [$email])
        ->first();
    if ($existing) {
        throw new RuntimeException("A user with email {$email} already exists (id {$existing->id}).");
    }

    $role = ensure_agent_role($roleName);

    $user = app(BookStack\Users\UserRepo::class)->create([
        'name' => $name,
        'email' => $email,
        'password' => $password,
        'roles' => [$role->id],
    ], false);

    $secret = Illuminate\Support\Str::random(32);
    $tokenId = Illuminate\Support\Str::random(32);
    while (BookStack\Api\ApiToken::query()->where('token_id', '=', $tokenId)->exists()) {
        $tokenId = Illuminate\Support\Str::random(32);
    }

    $token = (new BookStack\Api\ApiToken())->forceFill([
        'name' => 'fleet',
        'token_id' => $tokenId,
        'secret' => Illuminate\Support\Facades\Hash::make($secret),
        'user_id' => $user->id,
        'expires_at' => BookStack\Api\ApiToken::defaultExpiry(),
    ]);
    $token->save();

    return [
        'user_id' => (string) $user->id,
        'email' => $user->email,
        'name' => $user->name,
        'role' => $role->display_name,
        'role_id' => (string) $role->id,
        'password' => $password,
        'token_id' => $tokenId,
        'token_secret' => $secret,
        'authorization' => 'Token ' . $tokenId . ':' . $secret,
    ];
}

function ensure_agent_role(string $roleName): BookStack\Users\Models\Role
{
    $role = BookStack\Users\Models\Role::getRole($roleName);
    if ($role) {
        if (!$role->hasPermission('access-api')) {
            throw new RuntimeException(
                "Role \"{$roleName}\" exists but cannot call the API (missing access-api). " .
                "Leave --role unset to create an Agent role, or add access-api to this role in BookStack first."
            );
        }
        return $role;
    }

    $editor = BookStack\Users\Models\Role::getSystemRole('editor');
    if (!$editor) {
        throw new RuntimeException('The built-in Editor role is missing; cannot clone permissions.');
    }

    $permissions = $editor->permissions()->pluck('name')->all();
    if (!in_array('access-api', $permissions, true)) {
        $permissions[] = 'access-api';
    }

    return app(BookStack\Permissions\PermissionsRepo::class)->saveNewRole([
        'display_name' => $roleName,
        'description' => 'Fleet agent. Editor content permissions, plus API access.',
        'external_auth_id' => '',
        'permissions' => $permissions,
    ]);
}
