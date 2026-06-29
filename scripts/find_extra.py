import pandas as pd

df1 = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_14A.parquet')
df2 = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_14A_backup.parquet')

cols1 = set([c for c in df1.columns if not c.startswith('TARGET_')])
cols2 = set([c for c in df2.columns if not c.startswith('TARGET_')])

extra_in_new = cols1 - cols2
extra_in_old = cols2 - cols1

print('Extra columns in new 14A:', extra_in_new)
print('Extra columns in old 14A:', extra_in_old)
