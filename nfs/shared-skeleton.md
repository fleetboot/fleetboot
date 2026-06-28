# Shared directory layout

`/export/shared/` is exported with `sec=krb5p`, and inside it the **POSIX
permissions enforce who-sees-what**. Anyone with a valid Kerberos ticket
can mount; only directories whose POSIX perms allow their group can be
read or written. This is how we get "shared with teachers" without a
separate export.

A sensible starting layout, owned by the FreeIPA groups created during
enrolment:

| Path                          | Owner / mode                     | Audience |
|-------------------------------|----------------------------------|----------|
| `/export/shared/all`          | `root:root` mode `1777`          | All users, sticky. |
| `/export/shared/teachers`     | `root:teachers` mode `2770`      | Members of `teachers`. |
| `/export/shared/headmaster`   | `root:headmaster` mode `2770`    | Members of `headmaster`. |
| `/export/shared/students`     | `root:students` mode `2775`      | Members of `students`, read-only for non-members. |
| `/export/shared/coursework`   | `root:teachers` mode `2775`      | Teachers write, students read. |

The setgid bit on group-owned dirs (mode prefix `2`) means new files
inherit the parent's group — necessary for shared workflows. The sticky
bit on `all` mode `1777` means users can only delete their own files in
the otherwise-everyone-writable area.

Create the directory structure on first server setup; subsequent
admin curation happens in-tree like any other shared filesystem.
