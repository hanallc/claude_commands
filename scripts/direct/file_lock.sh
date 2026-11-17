# Lock an open descriptor with Perl on Linux and macOS.
# Find perl once, when a script sources this file. Look past PATH, which a caller can restrict.
FILE_LOCK_PERL=""
for file_lock_candidate in "$(command -v perl)" /run/current-system/sw/bin/perl /usr/bin/perl; do
  [[ -n "$file_lock_candidate" && -x "$file_lock_candidate" ]] && { FILE_LOCK_PERL="$file_lock_candidate"; break; }
done
unset file_lock_candidate

# The numbers 2 and 4 are LOCK_EX and LOCK_NB on Linux and macOS. A compile-time
# load of Fcntl or Errno costs five times the lock, so only a refusal loads Errno.
lock_file_descriptor() {
  [[ -n "$FILE_LOCK_PERL" ]] || return 2
  "$FILE_LOCK_PERL" -e 'open my $guard, ">&=", $ARGV[0] or exit 2;
flock($guard, 2 | ($ARGV[1] eq "nonblocking" ? 4 : 0)) and exit 0;
my $error = $! + 0;
require Errno;
exit($error == Errno::EWOULDBLOCK() || $error == Errno::EAGAIN() ? 75 : 2);' "$1" "${2:-blocking}"
}
