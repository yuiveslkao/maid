using System.Reflection;

namespace Maid;

/// <summary>exe（や dll）に埋め込んだファイル。</summary>
public static class Resources
{
    public static string Text(string name, Assembly? from = null)
    {
        using var s = (from ?? typeof(Resources).Assembly).GetManifestResourceStream(name)
                      ?? throw new FileNotFoundException(name);
        using var r = new StreamReader(s);
        return r.ReadToEnd();
    }
}
