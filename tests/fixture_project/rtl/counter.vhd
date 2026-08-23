library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity counter is
  generic (
    width : positive := 8
  );
  port (
    clk   : in  std_logic;
    reset : in  std_logic;
    inc   : in  std_logic;
    count : out std_logic_vector(width - 1 downto 0)
  );
end entity counter;

architecture rtl of counter is
  signal count_r : std_logic_vector(width - 1 downto 0) := (others => '0');
begin
  process (clk, reset)
  begin
    if reset = '1' then
      count_r <= (others => '0');
    elsif rising_edge(clk) then
      if inc = '1' then
        count_r <= std_logic_vector(unsigned(count_r) + 1);
      end if;
    end if;
  end process;

  count <= count_r;
end architecture rtl;
