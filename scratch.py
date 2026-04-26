import codecs

p = 'gcp/run_canary_batch.ps1'
with codecs.open(p, 'r', 'utf-8-sig') as f:
    d = f.read()

target1 = '''        TargetShort  = if ($exp.target_short)        { $exp.target_short        } else { ""                           }'''
replacement1 = '''        TargetShort  = if ($exp.target_short)        { $exp.target_short        } else { ""                           }\r\n        UseBuckets   = if ($exp.use_buckets -ne $null) { $exp.use_buckets       } else { $defaults.use_buckets        }'''

if target1 in d:
    d = d.replace(target1, replacement1)
else:
    print("Failed to find target1")

target2 = '''        if ($exp.TargetLong)  { $deployArgs += @("-TargetLong",  $exp.TargetLong)  }\r\n        if ($exp.TargetShort) { $deployArgs += @("-TargetShort", $exp.TargetShort) }'''
replacement2 = '''        if ($exp.TargetLong)  { $deployArgs += @("-TargetLong",  $exp.TargetLong)  }\r\n        if ($exp.TargetShort) { $deployArgs += @("-TargetShort", $exp.TargetShort) }\r\n        if ($exp.UseBuckets)  { $deployArgs += @("-UseBuckets") }'''

if target2 in d:
    d = d.replace(target2, replacement2)
else:
    print("Failed to find target2")

with codecs.open(p, 'w', 'utf-8-sig') as f:
    f.write(d)
print("Done")
