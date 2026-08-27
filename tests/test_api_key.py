#!/usr/bin/env python3
"""
CaSee MCP Server API Key 可用性测试

测试目标: 验证生产环境 MCP 服务使用指定 API Key 的可用性
测试服务: http://121.43.113.132:8100/mcp
API Key: casee_uvE3pVB9_atUmUD9CLm0wSCG7h8LS55yEwIWdcYoKGPqubFVDTVeMUftV
"""

import requests
import json
import time
import sys

# 配置
SERVER_URL = "http://121.43.113.132:8100/mcp"
API_KEY = "casee_uvE3pVB9_atUmUD9CLm0wSCG7h8LS55yEwIWdcYoKGPqubFVDTVeMUftV"
CASEE_API_BASE_URL = "http://121.43.113.132:8000"

# 测试结果存储
test_results = []
session_id = None

def log_result(test_name, status, message, duration=0):
    """记录测试结果"""
    result = {
        "test": test_name,
        "status": status,
        "message": message,
        "duration_ms": round(duration * 1000, 2)
    }
    test_results.append(result)
    icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
    print(f"  {icon} [{status}] {test_name}: {message} ({result['duration_ms']}ms)")
    return result

def mcp_initialize():
    """初始化 MCP 会话"""
    global session_id
    print("\n" + "="*60)
    print("测试 1: MCP 会话初始化")
    print("="*60)
    
    start = time.time()
    try:
        response = requests.post(
            SERVER_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "api-key-test", "version": "1.0"}
                }
            },
            timeout=10
        )
        
        duration = time.time() - start
        
        # 检查响应
        session_id = response.headers.get("Mcp-Session-Id", "")
        
        if response.status_code == 200 and session_id:
            # 解析响应
            init_data = {}
            for line in response.text.split("\n"):
                if line.startswith("data:"):
                    try:
                        init_data = json.loads(line[5:].strip())
                    except:
                        pass
            
            server_info = init_data.get("result", {}).get("serverInfo", {})
            log_result("MCP Initialize", "PASS", 
                      f"Session创建成功, Session ID: {session_id[:20]}...", duration)
            log_result("Server Info", "PASS", 
                      f"Name: {server_info.get('name', 'N/A')}, Version: {server_info.get('version', 'N/A')}", duration)
            return True
        else:
            log_result("MCP Initialize", "FAIL", 
                      f"HTTP {response.status_code}, 无 Session ID", duration)
            return False
            
    except Exception as e:
        duration = time.time() - start
        log_result("MCP Initialize", "FAIL", f"异常: {str(e)}", duration)
        return False

def mcp_list_tools():
    """列出可用工具"""
    global session_id
    print("\n" + "="*60)
    print("测试 2: 列出 MCP 工具")
    print("="*60)
    
    start = time.time()
    try:
        response = requests.post(
            SERVER_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {}
            },
            timeout=10
        )
        
        duration = time.time() - start
        
        # 解析工具列表
        tools_data = {}
        for line in response.text.split("\n"):
            if line.startswith("data:"):
                try:
                    tools_data = json.loads(line[5:].strip())
                except:
                    pass
        
        tools = tools_data.get("result", {}).get("tools", [])
        
        if response.status_code == 200 and len(tools) > 0:
            log_result("Tools List", "PASS", f"获取到 {len(tools)} 个工具", duration)
            
            print("\n  可用工具列表:")
            for tool in tools:
                print(f"    • {tool['name']}")
            
            return True
        else:
            log_result("Tools List", "FAIL", 
                      f"HTTP {response.status_code}, 工具数量: {len(tools)}", duration)
            return False
            
    except Exception as e:
        duration = time.time() - start
        log_result("Tools List", "FAIL", f"异常: {str(e)}", duration)
        return False

def mcp_call_tool(tool_name, arguments, test_name):
    """调用 MCP 工具"""
    global session_id
    print(f"\n  调用工具: {tool_name} ({test_name})")
    
    start = time.time()
    try:
        response = requests.post(
            SERVER_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments
                }
            },
            timeout=30
        )
        
        duration = time.time() - start
        
        # 解析响应
        result_data = {}
        for line in response.text.split("\n"):
            if line.startswith("data:"):
                try:
                    result_data = json.loads(line[5:].strip())
                except:
                    pass
        
        # 检查错误
        if "error" in result_data:
            error = result_data["error"]
            log_result(test_name, "FAIL", 
                      f"错误: {error.get('message', str(error))}", duration)
            return None
        
        # 提取输出内容
        content = result_data.get("result", {}).get("content", [])
        if content:
            text_content = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
            log_result(test_name, "PASS", 
                      f"调用成功, 返回内容长度: {len(text_content)} 字符", duration)
            return text_content
        else:
            log_result(test_name, "PASS", 
                      f"调用成功 (无内容)", duration)
            return ""
            
    except Exception as e:
        duration = time.time() - start
        log_result(test_name, "FAIL", f"异常: {str(e)}", duration)
        return None

def test_direct_api():
    """测试直接调用 CaSee API（验证 API Key）"""
    print("\n" + "="*60)
    print("测试 3: 直接调用 CaSee API 验证 API Key")
    print("="*60)
    
    start = time.time()
    try:
        # 调用 /intelligence/sources 接口验证 API Key
        response = requests.get(
            f"{CASEE_API_BASE_URL}/intelligence/sources",
            params={
                "keyword": "AI",
                "limit": 5
            },
            headers={
                "X-API-Key": API_KEY
            },
            timeout=10
        )
        
        duration = time.time() - start
        
        if response.status_code == 200:
            data = response.json()
            sources = data.get("sources", []) or data.get("data", []) or []
            log_result("API Key 验证 (直接调用)", "PASS", 
                      f"API 调用成功, 返回 {len(sources)} 个信源", duration)
            return True
        elif response.status_code == 401:
            log_result("API Key 验证 (直接调用)", "FAIL", 
                      "API Key 无效或已过期 (401 Unauthorized)", duration)
            return False
        else:
            log_result("API Key 验证 (直接调用)", "WARN", 
                      f"HTTP {response.status_code}, 响应: {response.text[:200]}", duration)
            return False
            
    except Exception as e:
        duration = time.time() - start
        log_result("API Key 验证 (直接调用)", "FAIL", f"异常: {str(e)}", duration)
        return False

def test_mcp_tool_find_sources():
    """测试信源检索工具"""
    print("\n" + "="*60)
    print("测试 4: MCP 信源检索 (find_trusted_sources)")
    print("="*60)
    
    result = mcp_call_tool(
        "find_trusted_sources",
        {
            "keyword": "人工智能",
            "min_tscore": 0.6,
            "limit": 5
        },
        "find_trusted_sources 调用"
    )
    
    if result:
        # 简单验证返回内容
        if len(result) > 10:
            log_result("信源检索结果", "PASS", 
                      f"返回有效内容 ({len(result)} 字符)")
        else:
            log_result("信源检索结果", "WARN", 
                      "返回内容较短")
        return True
    return False

def test_mcp_tool_search_intelligence():
    """测试情报检索工具"""
    print("\n" + "="*60)
    print("测试 5: MCP 情报检索 (search_intelligence)")
    print("="*60)
    
    result = mcp_call_tool(
        "search_intelligence",
        {
            "q": "人工智能 AND 半导体",
            "days": 30,
            "limit": 5
        },
        "search_intelligence 调用"
    )
    
    if result:
        if len(result) > 10:
            log_result("情报检索结果", "PASS", 
                      f"返回有效内容 ({len(result)} 字符)")
        else:
            log_result("情报检索结果", "WARN", 
                      "返回内容较短或无结果")
        return True
    return False

def test_mcp_tool_semantic_search():
    """测试语义检索工具"""
    print("\n" + "="*60)
    print("测试 6: MCP 语义检索 (semantic_search_tool)")
    print("="*60)
    
    result = mcp_call_tool(
        "semantic_search_tool",
        {
            "query": "人工智能芯片技术突破",
            "limit": 5
        },
        "semantic_search_tool 调用"
    )
    
    if result:
        if len(result) > 10:
            log_result("语义检索结果", "PASS", 
                      f"返回有效内容 ({len(result)} 字符)")
        else:
            log_result("语义检索结果", "WARN", 
                      "返回内容较短或无结果")
        return True
    return False

def print_summary():
    """打印测试总结"""
    print("\n" + "="*60)
    print("📊 测试总结")
    print("="*60)
    
    total = len(test_results)
    passed = sum(1 for r in test_results if r["status"] == "PASS")
    failed = sum(1 for r in test_results if r["status"] == "FAIL")
    warnings = sum(1 for r in test_results if r["status"] == "WARN")
    
    total_duration = sum(r["duration_ms"] for r in test_results)
    
    print(f"\n  总测试数: {total}")
    print(f"  ✅ 通过:   {passed}")
    print(f"  ❌ 失败:   {failed}")
    print(f"  ⚠️ 警告:   {warnings}")
    print(f"  📈 成功率: {passed/total*100:.1f}%")
    print(f"  ⏱️ 总耗时: {total_duration:.2f}ms")
    
    if failed == 0:
        print("\n  🎉 所有测试通过！API Key 有效，MCP 服务正常运行。")
        return True
    else:
        print("\n  ⚠️ 部分测试失败，请检查错误信息。")
        print("\n  失败项详情:")
        for r in test_results:
            if r["status"] == "FAIL":
                print(f"    ❌ {r['test']}: {r['message']}")
        return False

def main():
    """主测试流程"""
    print("="*60)
    print("🔍 CaSee MCP Server API Key 可用性测试")
    print("="*60)
    print(f"\n  服务地址: {SERVER_URL}")
    print(f"  API Key:  {API_KEY[:20]}...{API_KEY[-10:]}")
    print(f"  测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Step 1: 初始化 MCP 会话
    if not mcp_initialize():
        print("\n❌ MCP 会话初始化失败，终止测试")
        sys.exit(1)
    
    # Step 2: 列出工具
    if not mcp_list_tools():
        print("\n❌ 工具列表获取失败，终止测试")
        sys.exit(1)
    
    # Step 3: 直接测试 API Key
    test_direct_api()
    
    # Step 4-6: 测试 MCP 工具调用
    test_mcp_tool_find_sources()
    test_mcp_tool_search_intelligence()
    test_mcp_tool_semantic_search()
    
    # 打印总结
    success = print_summary()
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
