# Drug Registry

医药监管公开信息与药品说明书更新采集链路的离线实现。

## 单一职责

- `sources.py`：读取公告和说明书版本输入。
- `normalize.py`：统一药品名、企业名和批准文号。
- `diff.py`：执行段落级版本比较，输出新增/删除变化。
- `storage.py`：保存文档版本和变化记录。
- `pipeline.py`：编排处理流程。

```bash
PYTHONPATH=src python3 -m drug_registry.cli --fixture fixtures/notices.json --database /tmp/drug-registry.sqlite3
```
