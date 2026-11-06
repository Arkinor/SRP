
import json
try:
    import wmi
    c = wmi.WMI(namespace='root/Microsoft/Windows/Storage')
    print('storage-ok')
except Exception as e:
    print('storage-fail', type(e).__name__, e)
