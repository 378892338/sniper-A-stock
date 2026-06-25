# 012 `data/convert.py` Qlib 转换逻辑有误且未被使用

**严重程度**: LOW  
**文件**: `data/convert.py:23-31`  
**类型**: 死代码 / 逻辑错误

## 问题描述

`to_qlib_format` 的映射逻辑是反的：

```python
def to_qlib_format(df):
    mapping = {v: k for k, v in QLIB_COLUMNS.items()}
    # QLIB_COLUMNS = {"open": "$open", "close": "$close", ...}
    # mapping = {"$open": "open", "$close": "close", ...}
```

这会把 `$open` → `open`，方向反了。而且后续 `write_qlib_features` 检查 `col.startswith("$")` 来保存特征列，改名后就没有 `$` 前缀的列了。

同时搜索整个项目，这两个函数没有被任何地方调用——是死代码。

## 建议

如果未来要对接 Qlib，重写此模块；否则直接删除 `data/convert.py`。
