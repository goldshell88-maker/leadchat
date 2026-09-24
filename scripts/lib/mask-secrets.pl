# Masks credentials in text. Run with `perl -0777 -p`, so that every record is
# a whole file or the whole input stream. With -i the files are rewritten in
# place and "<replacements>\t<file>" is printed for every file that changed.

my $key = qr/[\w.-]*(?:secret|passw(?:or)?d|pwd|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)[\w.-]*/i;
my $n = 0;

# PEM private keys
$n += s/-----BEGIN ([A-Z ]*)PRIVATE KEY-----.*?-----END \1PRIVATE KEY-----/<REDACTED>/gs;

# Well-known token formats: Telegram bot, OpenAI/Anthropic, AWS, GitHub, Google, Slack, JWT
$n += s/(?<!\d)\d{8,10}:[\w-]{30,}/<REDACTED>/g;
$n += s/\b(?:sk-[\w-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_\w{36,}|AIza[\w-]{35}|xox[abprs]-[\w-]{10,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,})/<REDACTED>/g;

# Passwords in connection strings: scheme://user:password@host
$n += s{(\b[a-z][a-z0-9+.-]*://[^\s:/\@"'`]+:)[^\s\@"'`/]+\@}{$1<REDACTED>\@}gi;

# Quoted literals assigned to secret-like names: token: "...", PASSWORD = '...'
$n += s/((?:$key)["']?\s*[:=]{1,3}\s*["'])(?!<REDACTED>)[^"'\s]{6,}(["'])/$1<REDACTED>$2/g;

# Unquoted values in configs, shell scripts and command lines: PASSWORD=..., token: ...
if ($ARGV eq '-' || $ARGV =~ m{(?:\.(?:sh|bash|ya?ml|ini|conf|cfg|toml|properties|service)|/Dockerfile[^/]*)$}) {
    $n += s/((?:$key)[ \t]*[:=][ \t]*)(?![\$\{<])[^\s"'#,;]{6,}/$1<REDACTED>/g;
}

print STDOUT "$n\t$ARGV\n" if $n && $ARGV ne '-';
