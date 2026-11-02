"""Bounded ELF64 symbol reader for offline AArch64 symbol resolution."""
import struct


class ELF:
    def __init__(self, data):
        if not isinstance(data, bytes) or not 64 <= len(data) <= 64*1024*1024:
            raise ValueError('ELF 大小无效')
        self.data = data
        if data[:6] != b'\x7fELF\x02\x01':
            raise ValueError('仅支持小端 ELF64')
        header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
        if header[2] != 183 or header[1] not in (2, 3):
            raise ValueError('仅支持 AArch64 可执行文件或共享库')
        self.type = header[1]
        self.loads = []
        phoff, phsize, phcount = header[5], header[9], header[10]
        if phcount:
            if phsize != 56 or phcount > 4096:
                raise ValueError('ELF 程序头无效')
            self.slice(phoff,phsize*phcount)
            for i in range(phcount):
                segment=struct.unpack_from('<IIQQQQQQ',data,phoff+i*phsize)
                if segment[0]==1:
                    self.loads.append({'offset':segment[2],'vaddr':segment[3],'filesz':segment[5],'memsz':segment[6]})
        offset, size, count = header[6], header[11], header[12]
        if size != 64 or not 0 < count <= 65535:
            raise ValueError('ELF 节表无效')
        self.slice(offset, count*size)
        sections = [struct.unpack_from('<IIQQQQIIQQ', data, offset+i*size) for i in range(count)]
        self.build_id = None
        self.symbols = []
        for section in sections:
            kind, start, length = section[1], section[4], section[5]
            if kind == 7:
                notes = self.slice(start, length); cursor = 0
                while cursor+12 <= len(notes):
                    namesz, descsz, typ = struct.unpack_from('<III', notes, cursor);cursor += 12
                    end_name=cursor+namesz;desc_start=cursor+((namesz+3)&~3);end_desc=desc_start+descsz
                    if end_desc > len(notes):raise ValueError('ELF note 越界')
                    if notes[cursor:end_name].rstrip(b'\0')==b'GNU' and typ==3:
                        self.build_id=notes[desc_start:end_desc].hex()
                    cursor=desc_start+((descsz+3)&~3)
            if kind not in (2, 11):continue
            if section[9] != 24 or length % 24 or section[6] >= count:
                raise ValueError('ELF 符号表无效')
            strings_section=sections[section[6]]
            strings=self.slice(strings_section[4],strings_section[5])
            symbols=self.slice(start,length)
            for pos in range(0,length,24):
                name,info,other,index,value,span=struct.unpack_from('<IBBHQQ',symbols,pos)
                if info&15 != 2 or not index or not span:continue
                if name >= len(strings):raise ValueError('ELF 符号名称越界')
                end=strings.find(b'\0',name)
                if end < 0:raise ValueError('ELF 符号名称无终止符')
                self.symbols.append((value,span,strings[name:end].decode('utf-8','replace')))
        self.symbols.sort()

    def slice(self, offset, size):
        if offset < 0 or size < 0 or offset+size > len(self.data):
            raise ValueError('ELF 数据越界')
        return self.data[offset:offset+size]

    def resolve(self, address, load_bias, expected_build_id):
        if type(address) is not int or type(load_bias) is not int or address < 0 or load_bias < 0:
            raise ValueError('地址或加载基址无效')
        result={'address':hex(address),'symbol':None,'offset':None,'status':'unresolved'}
        if not expected_build_id or self.build_id != expected_build_id:
            return {**result,'status':'build-id-mismatch'}
        relative=address-load_bias
        for start,size,name in reversed(self.symbols):
            if start <= relative < start+size:
                return {**result,'symbol':name,'offset':relative-start,'status':'resolved'}
        return result

    def resolve_mapping(self, address, mapping, expected_build_id):
        start,end,offset=mapping['start'],mapping['end'],mapping['offset']
        if any(type(n) is not int or n<0 for n in (address,start,end,offset)) or end<=start:
            raise ValueError('进程映射无效')
        unknown={'address':hex(address),'symbol':None,'offset':None,'status':'unresolved'}
        if not start<=address<end:return unknown
        for segment in self.loads:
            # Linux maps ELF segments at page-aligned offsets/virtual addresses.
            file_page=segment['offset']&~4095
            virtual_page=segment['vaddr']&~4095
            limit=segment['offset']+segment['filesz']
            if file_page<=offset<limit:
                bias=start-(virtual_page+offset-file_page)
                if bias>=0:return self.resolve(address,bias,expected_build_id)
        return unknown
