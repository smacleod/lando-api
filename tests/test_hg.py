import io
from landoapi.hg import HgCommandError, HgRepo, NoDiffStartLine, PatchConflict


PATCH_WITHOUT_STARTLINE = rb"""
# HG changeset patch
# User Test User <test@example.com>
# Date 0 0
#      Thu Jan 01 00:00:00 1970 +0000
add another file.
diff --git a/test.txt b/test.txt
--- a/test.txt
+++ b/test.txt
@@ -1,1 +1,2 @@
 TEST
+adding another line
""".strip()


PATCH_WITH_CONFLICT = rb"""
# HG changeset patch
# User Test User <test@example.com>
# Date 0 0
#      Thu Jan 01 00:00:00 1970 +0000
# Diff Start Line 7
Add to a file that doesn't exist
diff --git a/not-real.txt b/not-real.txt
--- a/not-real.txt
+++ b/not-real.txt
@@ -1,1 +1,2 @@
 TEST
+This line doesn't exist
""".strip()


PATCH_ADD_NO_NEWLINE_FILE = rb"""
# HG changeset patch
# User Test User <test@example.com>
# Date 0 0
#      Thu Jan 01 00:00:00 1970 +0000
# Diff Start Line 7
file added
diff --git a/test-newline-file b/test-newline-file
new file mode 100644
--- /dev/null
+++ b/test-newline-file
@@ -0,0 +1,1 @@
+hello
\ No newline at end of file
""".strip()


PATCH_NORMAL = rb"""
# HG changeset patch
# User Test User <test@example.com>
# Date 0 0
#      Thu Jan 01 00:00:00 1970 +0000
# Diff Start Line 7
add another file.
diff --git a/test.txt b/test.txt
--- a/test.txt
+++ b/test.txt
@@ -1,1 +1,2 @@
 TEST
+adding another line
""".strip()


def test_integrated_hgrepo_apply_patch(hg_clone):
    repo = HgRepo(hg_clone.strpath)

    # We should refuse to apply patches that are missing a
    # Diff Start Line header.
    with pytest.raises(NoDiffStartLine), repo:
        repo.apply_patch(io.BytesIO(PATCH_WITHOUT_STARTLINE))

    # Patches with conflicts should raise a proper PatchConflict exception.
    with pytest.raises(PatchConflict), repo:
        repo.apply_patch(io.BytesIO(PATCH_WITH_CONFLICT))

    # Patches that fail to be applied by the default import should
    # also be tried using import with the patch command.
    with repo:
        repo.apply_patch(io.BytesIO(PATCH_ADD_NO_NEWLINE_FILE))
        # Commit created.
        assert repo.run_hg_cmds([["outgoing"]])

    with repo:
        repo.apply_patch(io.BytesIO(PATCH_NORMAL))
        # Commit created.
        assert repo.run_hg_cmds([["outgoing"]])