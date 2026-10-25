# 第三方声明

## cninfo-mcp

部分公告查询、字段处理和附件下载逻辑参考并改编自：

- 项目：[youhaozhao/cninfo-mcp](https://github.com/youhaozhao/cninfo-mcp)
- 固定提交：`90723c078af902e42f9b8be0ea476fa688c585f3`
- 来源文件：[python/spider.py](https://github.com/youhaozhao/cninfo-mcp/blob/90723c078af902e42f9b8be0ea476fa688c585f3/python/spider.py)
- 版权：Copyright (c) 2026 youhaozhao
- 许可证：MIT，完整文本见 [LICENSE.cninfo-mcp](LICENSE.cninfo-mcp)。

改编涉及完整中文年报筛选、版本选择、公司身份核对、串行限速、PDF 校验和本地状态管理。

## 运行依赖

依赖通过 Python 包管理器安装，分别适用其发行版附带的许可证：

| 依赖 | 许可证 | 项目 |
| --- | --- | --- |
| Requests | Apache-2.0 | [psf/requests](https://github.com/psf/requests) |
| pypdf | BSD-3-Clause | [py-pdf/pypdf](https://github.com/py-pdf/pypdf) |

## 数据来源

公告元数据及 PDF 来源于[巨潮资讯网](https://www.cninfo.com.cn/)。软件许可证不授予公告内容的所有权或其他数据使用权利。
