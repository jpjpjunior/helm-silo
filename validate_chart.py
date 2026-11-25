# 1. Reset the partial edits from the interrupted run
git checkout -- .

# 2. Add the runtimeClassName line to that snippet in apply_patches.py
python3 - <<'EOF'
p = "apply_patches.py"
s = open(p).read()
old = '''    replace_once(
        "templates/silo-deployment.yaml",
        "      {{- if .Values.silo.transcode.nvidia.enabled }}",
        "      {{- if and .Values.silo.transcode.nvidia.enabled .Values.silo.transcode.nvidia.runtimeClassName }}",
    )'''
new = '''    replace_once(
        "templates/silo-deployment.yaml",
        """      {{- if .Values.silo.transcode.nvidia.enabled }}
      runtimeClassName: {{ .Values.silo.transcode.nvidia.runtimeClassName }}""",
        """      {{- if and .Values.silo.transcode.nvidia.enabled .Values.silo.transcode.nvidia.runtimeClassName }}
      runtimeClassName: {{ .Values.silo.transcode.nvidia.runtimeClassName }}""",
    )'''
assert old in s, "snippet not found"
open(p, "w").write(s.replace(old, new, 1))
print("Updated apply_patches.py")
EOF

# 3. Run the script and verify
python3 apply_patches.py
rm apply_patches.py
git diff --stat
helm lint . -f ci/test-values/a-defaults.yaml
python3 ci/validate_chart.py
