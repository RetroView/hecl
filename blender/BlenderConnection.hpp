#ifndef BLENDERCONNECTION_HPP
#define BLENDERCONNECTION_HPP

#if _WIN32
#define _WIN32_LEAN_AND_MEAN 1
#include <windows.h>
#else
#include <unistd.h>
#endif

#include <stdint.h>
#include <string>
#include <functional>
#include <mutex>

#include "HECL/HECL.hpp"

namespace HECL
{

extern LogVisor::LogModule BlenderLog;
extern class BlenderConnection* SharedBlenderConnection;

class BlenderConnection
{
    std::mutex m_lock;
#if _WIN32
    HANDLE m_blenderProc;
    HANDLE m_readpipe[2];
    HANDLE m_writepipe[2];
#else
    pid_t m_blenderProc;
    int m_readpipe[2];
    int m_writepipe[2];
#endif
    std::string m_loadedBlend;
    size_t _readLine(char* buf, size_t bufSz);
    size_t _writeLine(const char* buf);
    size_t _readBuf(char* buf, size_t len);
    size_t _writeBuf(const char* buf, size_t len);
    void _closePipe();
public:
    BlenderConnection(bool silenceBlender=false);
    ~BlenderConnection();

    bool createBlend(const SystemString& path);
    bool openBlend(const SystemString& path);
    enum CookPlatform
    {
        CP_MODERN = 0,
        CP_GX     = 1,
    };
    bool cookBlend(std::function<char*(uint32_t)> bufGetter,
                   const std::string& expectedType,
                   const std::string& platform,
                   bool bigEndian=false);

    class PyOutStream : public std::ostream
    {
        friend class BlenderConnection;
        std::unique_lock<std::mutex> m_lk;
        BlenderConnection* m_parent;
        struct StreamBuf : std::streambuf
        {
            BlenderConnection* m_parent;
            std::string m_lineBuf;
            StreamBuf(BlenderConnection* parent) : m_parent(parent) {}
            StreamBuf(const StreamBuf& other) = delete;
            StreamBuf(StreamBuf&& other) = default;
            int_type overflow(int_type ch)
            {
                if (ch != traits_type::eof() && ch != '\n')
                {
                    m_lineBuf += char_type(ch);
                    return ch;
                }
                m_parent->_writeLine(m_lineBuf.c_str());
                char readBuf[16];
                m_parent->_readLine(readBuf, 16);
                if (strcmp(readBuf, "OK"))
                    BlenderLog.report(LogVisor::FatalError, "error sending '%s' to blender", m_lineBuf.c_str());
                m_lineBuf.clear();
                return ch;
            }
        } m_sbuf;
        PyOutStream(BlenderConnection* parent)
        : m_lk(parent->m_lock), m_parent(parent), m_sbuf(parent), std::ostream(&m_sbuf)
        {
            m_parent->_writeLine("PYBEGIN");
            char readBuf[16];
            m_parent->_readLine(readBuf, 16);
            if (strcmp(readBuf, "READY"))
                BlenderLog.report(LogVisor::FatalError, "unable to open PyOutStream with blender");
        }
    public:
        PyOutStream(const PyOutStream& other) = delete;
        PyOutStream(PyOutStream&& other)
        : m_lk(std::move(other.m_lk)), m_parent(other.m_parent), m_sbuf(std::move(other.m_sbuf))
        {other.m_parent = nullptr;}
        ~PyOutStream()
        {
            if (m_parent)
            {
                m_parent->_writeLine("PYEND");
                char readBuf[16];
                m_parent->_readLine(readBuf, 16);
                if (strcmp(readBuf, "DONE"))
                    BlenderLog.report(LogVisor::FatalError, "unable to close PyOutStream with blender");
            }
        }
    };
    inline PyOutStream beginPythonOut()
    {
        return PyOutStream(this);
    }

    void quitBlender();

    static inline BlenderConnection& SharedConnection()
    {
        if (!SharedBlenderConnection)
            SharedBlenderConnection = new BlenderConnection();
        return *SharedBlenderConnection;
    }

    static inline void Shutdown()
    {
        delete SharedBlenderConnection;
    }
};

}

#endif // BLENDERCONNECTION_HPP