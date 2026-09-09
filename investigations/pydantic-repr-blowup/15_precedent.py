"""Do other rich-repr libraries truncate by default? Same data volume to each."""
import time
import numpy as np, pandas as pd
from pydantic import BaseModel

N = 5_000_000

def bench(label, fn):
    t0 = time.monotonic(); out = fn(); el = time.monotonic()-t0
    print("  %-34s %8.3fs %12s chars   %s" % (label, el, "{:,}".format(len(out)),
                                              out[:52].replace("\n", " ") + "..."))

print("== %s elements, repr() of each container ==" % "{:,}".format(N))
bench("numpy ndarray", lambda: repr(np.arange(N)))
bench("pandas Series", lambda: repr(pd.Series(np.arange(N))))
bench("pandas DataFrame", lambda: repr(pd.DataFrame({"a": np.arange(N)})))

class M(BaseModel):
    items: list
bench("pydantic BaseModel", lambda: repr(M(items=list(range(N)))))
bench("plain list (a primitive)", lambda: repr(list(range(N))))

print()
print("== their knobs ==")
print("  numpy  np.get_printoptions()['threshold'] =", np.get_printoptions()["threshold"])
print("  pandas pd.get_option('display.max_rows')  =", pd.get_option("display.max_rows"))
print("  pydantic                                  = no repr length/depth option")
