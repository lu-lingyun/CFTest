MAX_CONCURRENT_THREADS = 1000
REQUEST_TIMEOUT = 3

import sys
import requests
import ipaddress
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import re
import threading
import argparse
from collections import defaultdict

def is_valid_ipv4_range(ip_range):
    """验证IPv4段格式是否正确"""
    try:
        network = ipaddress.ip_network(ip_range, strict=True)
        return isinstance(network, ipaddress.IPv4Network)
    except ValueError:
        return False
def fetch_ip_ranges(url):
    """从指定URL获取IP段列表"""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        ip_ranges = []
        for line in response.text.splitlines():
            line = line.strip()
            if line and is_valid_ipv4_range(line):
                ip_ranges.append(line)
            elif line:
                print(f"忽略无效的IP段: {line}")
                
        return ip_ranges
    except Exception as e:
        print(f"获取IP段失败: {e}")
        sys.exit(1)

def expand_ip_range(ip_range):
    """将IP段扩展为具体的所有IP地址列表"""
    try:
        network = ipaddress.ip_network(ip_range, strict=True)
        
        if not isinstance(network, ipaddress.IPv4Network):
            print(f"忽略非IPv4段: {ip_range}")
            return []
            
        return [str(ip) for ip in network]
            
    except ValueError as e:
        print(f"解析IP段 {ip_range} 失败: {e}")
        return []

def check_ip_location(ip, target_colos, stop_event):
    """检查IP是否连通，返回IP和对应的三字码（支持多地区筛选）"""
    if stop_event.is_set():
        return None
        
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        print(f"无效的IP地址: {ip}")
        return None
        
    url = f"http://{ip}/cdn-cgi/trace"
    try:
        if stop_event.is_set():
            return None
            
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        # 提取三字码
        colo = None
        for line in response.text.splitlines():
            if line.startswith('colo='):
                colo = line.split('=')[1].strip()
                break
                
        # 验证目标机场码（支持多个目标）
        if target_colos and colo not in target_colos:
            return None
                
        return (ip, colo) if colo else None
    except Exception:
        return None

def main():
    # 解析命令行参数（支持多个地区参数）
    parser = argparse.ArgumentParser(description='查找指定机场码或可连通的IP地址')
    parser.add_argument('-d', nargs='+', help='机场三字码（可选，可指定多个，不填则匹配所有可连通IP）')
    parser.add_argument('-i', type=int, default=10, help='测试数量，默认10个')
    parser.add_argument('-o', default='output.txt', help='输出文件名，默认output.txt')
    args = parser.parse_args()

    # 处理多个地区参数（转为大写）
    target_colos = [col.upper() for col in args.d] if args.d else None
    try:
        max_count = args.i
        if max_count <= 0:
            raise ValueError("最大数量必须为正数")
    except ValueError as e:
        print(f"无效的最大数量: {e}")
        sys.exit(1)
    
    output_file = args.o
    ip_ranges_url = "https://www.cloudflare-cn.com/ips-v4"
    
    print(f"正在从 {ip_ranges_url} 获取IP段...")
    ip_ranges = fetch_ip_ranges(ip_ranges_url)
    print(f"成功获取并验证 {len(ip_ranges)} 个IP段")
    
    print("正在扩展IP段...")
    all_ips = []
    for range_str in ip_ranges:
        ips = expand_ip_range(range_str)
        all_ips.extend(ips)
        print(f"  从 {range_str} 扩展出 {len(ips)} 个IP")
    
    all_ips = list(set(all_ips))
    # 调整搜索模式描述（支持多地区显示）
    if target_colos:
        search_mode = f"属于 {', '.join(target_colos)} 的IP"
    else:
        search_mode = "可连通的IP"
    print(f"共扩展出 {len(all_ips)} 个唯一IP地址，正在检查每个IP...")
    print(f"找到 {max_count} 个{search_mode}后将停止搜索")
    
    stop_event = threading.Event()
    max_workers = min(MAX_CONCURRENT_THREADS, len(all_ips))
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for ip in all_ips:
            # 传入多个目标地区
            future = executor.submit(check_ip_location, ip, target_colos, stop_event)
            futures.append(future)
        
        total = len(futures)
        completed = 0
        matched_results = []  # 存储(IP, 三字码)元组
        
        while completed < total and len(matched_results) < max_count:
            done, not_done = wait(futures, return_when=FIRST_COMPLETED)
            
            for future in done:
                if future in futures:
                    futures.remove(future)
                    completed += 1
                    
                    result = future.result()
                    if result:
                        matched_results.append(result)
                        print(f"已找到 {len(matched_results)}/{max_count} 个{search_mode}")
                        
                        if len(matched_results) >= max_count:
                            print(f"\n已找到 {max_count} 个{search_mode}，停止搜索")
                            stop_event.set()
                            for f in futures:
                                f.cancel()
                            break
            
            if stop_event.is_set():
                break
            
            if completed % 50 == 0 or completed == total:
                print(f"进度: {completed}/{total} ({(completed/total)*100:.1f}%)，已找到 {len(matched_results)} 个{search_mode}")
    
    if len(matched_results) > max_count:
        matched_results = matched_results[:max_count]
    
    # 按IP地址排序
    matched_results.sort(key=lambda x: ipaddress.IPv4Address(x[0]))
    
    # 按三字码分组并计数
    colo_groups = defaultdict(list)
    for ip, colo in matched_results:
        colo_groups[colo].append(ip)
    
    # 按三字码字母顺序排序并写入文件
    with open(output_file, 'w') as f:
        for colo in sorted(colo_groups.keys()):
            ips = colo_groups[colo]
            for idx, ip in enumerate(ips, 1):
                f.write(f"{ip}#{colo} {idx}\n")
    
    print(f"完成！共找到 {len(matched_results)} 个{search_mode}，已保存到 {output_file}")

main()