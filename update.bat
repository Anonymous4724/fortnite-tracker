@echo off
rem  Everything, in one go: a short harvest pass for the new tournaments, the
rem  model rebuilt and verified, the week's calendar, the page, and the push.
rem  Ten minutes or so. site.bat is the quick version when only the page or
rem  the calendar changed. Add --quiet for a scheduled task.
call "%~dp0refresh.bat" --fetch --publish %*
